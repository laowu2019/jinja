# -*- coding: utf-8 -*-
"""
    jinja2.lexer
    ~~~~~~~~~~~~

    This module implements a Jinja / Python combination lexer. The
    `Lexer` class provided by this module is used to do some preprocessing
    for Jinja.

    On the one hand it filters out invalid operators like the bitshift
    operators we don't allow in templates. On the other hand it separates
    template code and python code in expressions.

    :copyright: (c) 2009 by the Jinja Team.
    :license: BSD, see LICENSE for more details.
"""
import re
from operator import itemgetter
from collections import deque
from jinja2.exceptions import TemplateSyntaxError
from jinja2.utils import LRUCache


# cache for the lexers. Exists in order to be able to have multiple
# environments with the same lexer
_lexer_cache = LRUCache(50)

# static regular expressions
whitespace_re = re.compile(r'\s+', re.U)
string_re = re.compile(r"('([^'\\]*(?:\\.[^'\\]*)*)'"
                       r'|"([^"\\]*(?:\\.[^"\\]*)*)")', re.S)
integer_re = re.compile(r'\d+')
name_re = re.compile(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b')
float_re = re.compile(r'(?<!\.)\d+\.\d+')
newline_re = re.compile(r'(\r\n|\r|\n)')

# internal the tokens and keep references to them
TOKEN_ADD = intern('add')
TOKEN_ASSIGN = intern('assign')
TOKEN_COLON = intern('colon')
TOKEN_COMMA = intern('comma')
TOKEN_DIV = intern('div')
TOKEN_DOT = intern('dot')
TOKEN_EQ = intern('eq')
TOKEN_FLOORDIV = intern('floordiv')
TOKEN_GT = intern('gt')
TOKEN_GTEQ = intern('gteq')
TOKEN_LBRACE = intern('lbrace')
TOKEN_LBRACKET = intern('lbracket')
TOKEN_LPAREN = intern('lparen')
TOKEN_LT = intern('lt')
TOKEN_LTEQ = intern('lteq')
TOKEN_MOD = intern('mod')
TOKEN_MUL = intern('mul')
TOKEN_NE = intern('ne')
TOKEN_PIPE = intern('pipe')
TOKEN_POW = intern('pow')
TOKEN_RBRACE = intern('rbrace')
TOKEN_RBRACKET = intern('rbracket')
TOKEN_RPAREN = intern('rparen')
TOKEN_SEMICOLON = intern('semicolon')
TOKEN_SUB = intern('sub')
TOKEN_TILDE = intern('tilde')
TOKEN_WHITESPACE = intern('whitespace')
TOKEN_FLOAT = intern('float')
TOKEN_INTEGER = intern('integer')
TOKEN_NAME = intern('name')
TOKEN_STRING = intern('string')
TOKEN_OPERATOR = intern('operator')
TOKEN_BLOCK_BEGIN = intern('block_begin')
TOKEN_BLOCK_END = intern('block_end')
TOKEN_VARIABLE_BEGIN = intern('variable_begin')
TOKEN_VARIABLE_END = intern('variable_end')
TOKEN_RAW_BEGIN = intern('raw_begin')
TOKEN_RAW_END = intern('raw_end')
TOKEN_COMMENT_BEGIN = intern('comment_begin')
TOKEN_COMMENT_END = intern('comment_end')
TOKEN_COMMENT = intern('comment')
TOKEN_LINESTATEMENT_BEGIN = intern('linestatement_begin')
TOKEN_LINESTATEMENT_END = intern('linestatement_end')
TOKEN_DATA = intern('data')
TOKEN_INITIAL = intern('initial')
TOKEN_EOF = intern('eof')

# bind operators to token types
operators = {
    '+':            TOKEN_ADD,
    '-':            TOKEN_SUB,
    '/':            TOKEN_DIV,
    '//':           TOKEN_FLOORDIV,
    '*':            TOKEN_MUL,
    '%':            TOKEN_MOD,
    '**':           TOKEN_POW,
    '~':            TOKEN_TILDE,
    '[':            TOKEN_LBRACKET,
    ']':            TOKEN_RBRACKET,
    '(':            TOKEN_LPAREN,
    ')':            TOKEN_RPAREN,
    '{':            TOKEN_LBRACE,
    '}':            TOKEN_RBRACE,
    '==':           TOKEN_EQ,
    '!=':           TOKEN_NE,
    '>':            TOKEN_GT,
    '>=':           TOKEN_GTEQ,
    '<':            TOKEN_LT,
    '<=':           TOKEN_LTEQ,
    '=':            TOKEN_ASSIGN,
    '.':            TOKEN_DOT,
    ':':            TOKEN_COLON,
    '|':            TOKEN_PIPE,
    ',':            TOKEN_COMMA,
    ';':            TOKEN_SEMICOLON
}

reverse_operators = dict([(v, k) for k, v in operators.iteritems()])
assert len(operators) == len(reverse_operators), 'operators dropped'
operator_re = re.compile('(%s)' % '|'.join(re.escape(x) for x in
                         sorted(operators, key=lambda x: -len(x))))


def count_newlines(value):
    """Count the number of newline characters in the string.  This is
    useful for extensions that filter a stream.
    """
    return len(newline_re.findall(value))


class Failure(object):
    """Class that raises a `TemplateSyntaxError` if called.
    Used by the `Lexer` to specify known errors.
    """

    def __init__(self, message, cls=TemplateSyntaxError):
        self.message = message
        self.error_class = cls

    def __call__(self, lineno, filename):
        raise self.error_class(self.message, lineno, filename)


class Token(tuple):
    """Token class."""
    __slots__ = ()
    lineno, type, value = (property(itemgetter(x)) for x in range(3))

    def __new__(cls, lineno, type, value):
        return tuple.__new__(cls, (lineno, intern(str(type)), value))

    def __str__(self):
        if self.type in reverse_operators:
            return reverse_operators[self.type]
        elif self.type == 'name':
            return self.value
        return self.type

    def test(self, expr):
        """Test a token against a token expression.  This can either be a
        token type or ``'token_type:token_value'``.  This can only test
        against string values and types.
        """
        # here we do a regular string equality check as test_any is usually
        # passed an iterable of not interned strings.
        if self.type == expr:
            return True
        elif ':' in expr:
            return expr.split(':', 1) == [self.type, self.value]
        return False

    def test_any(self, *iterable):
        """Test against multiple token expressions."""
        for expr in iterable:
            if self.test(expr):
                return True
        return False

    def __repr__(self):
        return 'Token(%r, %r, %r)' % (
            self.lineno,
            self.type,
            self.value
        )


class TokenStreamIterator(object):
    """The iterator for tokenstreams.  Iterate over the stream
    until the eof token is reached.
    """

    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        return self

    def next(self):
        token = self.stream.current
        if token.type is TOKEN_EOF:
            self.stream.close()
            raise StopIteration()
        self.stream.next()
        return token


class TokenStream(object):
    """A token stream is an iterable that yields :class:`Token`\s.  The
    parser however does not iterate over it but calls :meth:`next` to go
    one token ahead.  The current active token is stored as :attr:`current`.
    """

    def __init__(self, generator, name, filename):
        self._next = iter(generator).next
        self._pushed = deque()
        self.name = name
        self.filename = filename
        self.closed = False
        self.current = Token(1, TOKEN_INITIAL, '')
        self.next()

    def __iter__(self):
        return TokenStreamIterator(self)

    def __nonzero__(self):
        """Are we at the end of the stream?"""
        return bool(self._pushed) or self.current.type is not TOKEN_EOF

    eos = property(lambda x: not x.__nonzero__(), doc=__nonzero__.__doc__)

    def push(self, token):
        """Push a token back to the stream."""
        self._pushed.append(token)

    def look(self):
        """Look at the next token."""
        old_token = self.next()
        result = self.current
        self.push(result)
        self.current = old_token
        return result

    def skip(self, n=1):
        """Got n tokens ahead."""
        for x in xrange(n):
            self.next()

    def next_if(self, expr):
        """Perform the token test and return the token if it matched.
        Otherwise the return value is `None`.
        """
        if self.current.test(expr):
            return self.next()

    def skip_if(self, expr):
        """Like :meth:`next_if` but only returns `True` or `False`."""
        return self.next_if(expr) is not None

    def next(self):
        """Go one token ahead and return the old one"""
        rv = self.current
        if self._pushed:
            self.current = self._pushed.popleft()
        elif self.current.type is not TOKEN_EOF:
            try:
                self.current = self._next()
            except StopIteration:
                self.close()
        return rv

    def close(self):
        """Close the stream."""
        self.current = Token(self.current.lineno, TOKEN_EOF, '')
        self._next = None
        self.closed = True

    def expect(self, expr):
        """Expect a given token type and return it.  This accepts the same
        argument as :meth:`jinja2.lexer.Token.test`.
        """
        if not self.current.test(expr):
            if ':' in expr:
                expr = expr.split(':')[1]
            if self.current.type is TOKEN_EOF:
                raise TemplateSyntaxError('unexpected end of template, '
                                          'expected %r.' % expr,
                                          self.current.lineno,
                                          self.name, self.filename)
            raise TemplateSyntaxError("expected token %r, got %r" %
                                      (expr, str(self.current)),
                                      self.current.lineno,
                                      self.name, self.filename)
        try:
            return self.current
        finally:
            self.next()


def get_lexer(environment):
    """Return a lexer which is probably cached."""
    key = (environment.block_start_string,
           environment.block_end_string,
           environment.variable_start_string,
           environment.variable_end_string,
           environment.comment_start_string,
           environment.comment_end_string,
           environment.line_statement_prefix,
           environment.trim_blocks,
           environment.newline_sequence)
    lexer = _lexer_cache.get(key)
    if lexer is None:
        lexer = Lexer(environment)
        _lexer_cache[key] = lexer
    return lexer


class Lexer(object):
    """Class that implements a lexer for a given environment. Automatically
    created by the environment class, usually you don't have to do that.

    Note that the lexer is not automatically bound to an environment.
    Multiple environments can share the same lexer.
    """

    def __init__(self, environment):
        # shortcuts
        c = lambda x: re.compile(x, re.M | re.S)
        e = re.escape

        # lexing rules for tags
        tag_rules = [
            (whitespace_re, TOKEN_WHITESPACE, None),
            (float_re, TOKEN_FLOAT, None),
            (integer_re, TOKEN_INTEGER, None),
            (name_re, TOKEN_NAME, None),
            (string_re, TOKEN_STRING, None),
            (operator_re, TOKEN_OPERATOR, None)
        ]

        # assamble the root lexing rule. because "|" is ungreedy
        # we have to sort by length so that the lexer continues working
        # as expected when we have parsing rules like <% for block and
        # <%= for variables. (if someone wants asp like syntax)
        # variables are just part of the rules if variable processing
        # is required.
        root_tag_rules = [
            ('comment',     environment.comment_start_string),
            ('block',       environment.block_start_string),
            ('variable',    environment.variable_start_string)
        ]
        root_tag_rules.sort(key=lambda x: -len(x[1]))

        # now escape the rules.  This is done here so that the escape
        # signs don't count for the lengths of the tags.
        root_tag_rules = [(a, e(b)) for a, b in root_tag_rules]

        # if we have a line statement prefix we need an extra rule for
        # that.  We add this rule *after* all the others.
        if environment.line_statement_prefix is not None:
            prefix = e(environment.line_statement_prefix)
            root_tag_rules.insert(0, ('linestatement', '^\s*' + prefix))

        # block suffix if trimming is enabled
        block_suffix_re = environment.trim_blocks and '\\n?' or ''

        self.newline_sequence = environment.newline_sequence

        # global lexing rules
        self.rules = {
            'root': [
                # directives
                (c('(.*?)(?:%s)' % '|'.join(
                    ['(?P<raw_begin>(?:\s*%s\-|%s)\s*raw\s*%s)' % (
                        e(environment.block_start_string),
                        e(environment.block_start_string),
                        e(environment.block_end_string)
                    )] + [
                        '(?P<%s_begin>\s*%s\-|%s)' % (n, r, r)
                        for n, r in root_tag_rules
                    ])), (TOKEN_DATA, '#bygroup'), '#bygroup'),
                # data
                (c('.+'), 'data', None)
            ],
            # comments
            TOKEN_COMMENT_BEGIN: [
                (c(r'(.*?)((?:\-%s\s*|%s)%s)' % (
                    e(environment.comment_end_string),
                    e(environment.comment_end_string),
                    block_suffix_re
                )), (TOKEN_COMMENT, TOKEN_COMMENT_END), '#pop'),
                (c('(.)'), (Failure('Missing end of comment tag'),), None)
            ],
            # blocks
            TOKEN_BLOCK_BEGIN: [
                (c('(?:\-%s\s*|%s)%s' % (
                    e(environment.block_end_string),
                    e(environment.block_end_string),
                    block_suffix_re
                )), TOKEN_BLOCK_END, '#pop'),
            ] + tag_rules,
            # variables
            TOKEN_VARIABLE_BEGIN: [
                (c('\-%s\s*|%s' % (
                    e(environment.variable_end_string),
                    e(environment.variable_end_string)
                )), TOKEN_VARIABLE_END, '#pop')
            ] + tag_rules,
            # raw block
            TOKEN_RAW_BEGIN: [
                (c('(.*?)((?:\s*%s\-|%s)\s*endraw\s*(?:\-%s\s*|%s%s))' % (
                    e(environment.block_start_string),
                    e(environment.block_start_string),
                    e(environment.block_end_string),
                    e(environment.block_end_string),
                    block_suffix_re
                )), (TOKEN_DATA, TOKEN_RAW_END), '#pop'),
                (c('(.)'), (Failure('Missing end of raw directive'),), None)
            ],
            # line statements
            TOKEN_LINESTATEMENT_BEGIN: [
                (c(r'\s*(\n|$)'), TOKEN_LINESTATEMENT_END, '#pop')
            ] + tag_rules
        }

    def _normalize_newlines(self, value):
        """Called for strings and template data to normlize it to unicode."""
        return newline_re.sub(self.newline_sequence, value)

    def tokenize(self, source, name=None, filename=None, state=None):
        """Calls tokeniter + tokenize and wraps it in a token stream.
        """
        stream = self.tokeniter(source, name, filename, state)
        return TokenStream(self.wrap(stream, name, filename), name, filename)

    def wrap(self, stream, name=None, filename=None):
        """This is called with the stream as returned by `tokenize` and wraps
        every token in a :class:`Token` and converts the value.
        """
        for lineno, token, value in stream:
            if token in ('comment_begin', 'comment', 'comment_end',
                         'whitespace'):
                continue
            elif token == 'linestatement_begin':
                token = 'block_begin'
            elif token == 'linestatement_end':
                token = 'block_end'
            # we are not interested in those tokens in the parser
            elif token in ('raw_begin', 'raw_end'):
                continue
            elif token == 'data':
                value = self._normalize_newlines(value)
            elif token == 'keyword':
                token = value
            elif token == 'name':
                value = str(value)
            elif token == 'string':
                # try to unescape string
                try:
                    value = self._normalize_newlines(value[1:-1]) \
                        .encode('ascii', 'backslashreplace') \
                        .decode('unicode-escape')
                except Exception, e:
                    msg = str(e).split(':')[-1].strip()
                    raise TemplateSyntaxError(msg, lineno, name, filename)
                # if we can express it as bytestring (ascii only)
                # we do that for support of semi broken APIs
                # as datetime.datetime.strftime
                try:
                    value = str(value)
                except UnicodeError:
                    pass
            elif token == 'integer':
                value = int(value)
            elif token == 'float':
                value = float(value)
            elif token == 'operator':
                token = operators[value]
            yield Token(lineno, token, value)

    def tokeniter(self, source, name, filename=None, state=None):
        """This method tokenizes the text and returns the tokens in a
        generator.  Use this method if you just want to tokenize a template.
        """
        source = '\n'.join(unicode(source).splitlines())
        pos = 0
        lineno = 1
        stack = ['root']
        if state is not None and state != 'root':
            assert state in ('variable', 'block'), 'invalid state'
            stack.append(state + '_begin')
        else:
            state = 'root'
        statetokens = self.rules[stack[-1]]
        source_length = len(source)

        balancing_stack = []

        while 1:
            # tokenizer loop
            for regex, tokens, new_state in statetokens:
                m = regex.match(source, pos)
                # if no match we try again with the next rule
                if m is None:
                    continue

                # we only match blocks and variables if brances / parentheses
                # are balanced. continue parsing with the lower rule which
                # is the operator rule. do this only if the end tags look
                # like operators
                if balancing_stack and \
                   tokens in ('variable_end', 'block_end',
                              'linestatement_end'):
                    continue

                # tuples support more options
                if isinstance(tokens, tuple):
                    for idx, token in enumerate(tokens):
                        # failure group
                        if token.__class__ is Failure:
                            raise token(lineno, filename)
                        # bygroup is a bit more complex, in that case we
                        # yield for the current token the first named
                        # group that matched
                        elif token == '#bygroup':
                            for key, value in m.groupdict().iteritems():
                                if value is not None:
                                    yield lineno, key, value
                                    lineno += value.count('\n')
                                    break
                            else:
                                raise RuntimeError('%r wanted to resolve '
                                                   'the token dynamically'
                                                   ' but no group matched'
                                                   % regex)
                        # normal group
                        else:
                            data = m.group(idx + 1)
                            if data:
                                yield lineno, token, data
                            lineno += data.count('\n')

                # strings as token just are yielded as it.
                else:
                    data = m.group()
                    # update brace/parentheses balance
                    if tokens == 'operator':
                        if data == '{':
                            balancing_stack.append('}')
                        elif data == '(':
                            balancing_stack.append(')')
                        elif data == '[':
                            balancing_stack.append(']')
                        elif data in ('}', ')', ']'):
                            if not balancing_stack:
                                raise TemplateSyntaxError('unexpected "%s"' %
                                                          data, lineno, name,
                                                          filename)
                            expected_op = balancing_stack.pop()
                            if expected_op != data:
                                raise TemplateSyntaxError('unexpected "%s", '
                                                          'expected "%s"' %
                                                          (data, expected_op),
                                                          lineno, name,
                                                          filename)
                    # yield items
                    yield lineno, tokens, data
                    lineno += data.count('\n')

                # fetch new position into new variable so that we can check
                # if there is a internal parsing error which would result
                # in an infinite loop
                pos2 = m.end()

                # handle state changes
                if new_state is not None:
                    # remove the uppermost state
                    if new_state == '#pop':
                        stack.pop()
                    # resolve the new state by group checking
                    elif new_state == '#bygroup':
                        for key, value in m.groupdict().iteritems():
                            if value is not None:
                                stack.append(key)
                                break
                        else:
                            raise RuntimeError('%r wanted to resolve the '
                                               'new state dynamically but'
                                               ' no group matched' %
                                               regex)
                    # direct state name given
                    else:
                        stack.append(new_state)
                    statetokens = self.rules[stack[-1]]
                # we are still at the same position and no stack change.
                # this means a loop without break condition, avoid that and
                # raise error
                elif pos2 == pos:
                    raise RuntimeError('%r yielded empty string without '
                                       'stack change' % regex)
                # publish new function and start again
                pos = pos2
                break
            # if loop terminated without break we havn't found a single match
            # either we are at the end of the file or we have a problem
            else:
                # end of text
                if pos >= source_length:
                    return
                # something went wrong
                raise TemplateSyntaxError('unexpected char %r at %d' %
                                          (source[pos], pos), lineno,
                                          name, filename)
