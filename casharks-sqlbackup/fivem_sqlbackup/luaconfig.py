"""Read and write config.lua.

Only a data subset of Lua is accepted (it is parsed, never executed):

    Config = {}
    Config.database = {
        host = "127.0.0.1",   -- comments are fine
        port = 3306,
    }
    Config.backup.databases = { "my_database" }   -- single settings may be set directly too

Values may be strings, numbers, true/false or lists of strings.
"""

import re

from .errors import ConfigError

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<lcomment>--\[(?P<eq>=*)\[.*?\](?P=eq)\])
  | (?P<comment>--[^\n]*)
  | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<lstring>\[(?P<eq2>=*)\[.*?\](?P=eq2)\])
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[{}=,.;\[\]])
""", re.S | re.X)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0"}


def _tokens(text: str):
    pos, line = 0, 1
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ConfigError(f"config.lua line {line}: unexpected character {text[pos]!r}")
        kind, value = None, m.group()
        for k in ("ws", "lcomment", "comment", "string", "lstring", "number", "name", "punct"):
            if m.group(k) is not None:
                kind = k
                break
        if kind not in ("ws", "lcomment", "comment"):
            yield kind, value, line
        line += value.count("\n")
        pos = m.end()
    yield "eof", "", line


def _unquote(raw: str) -> str:
    if raw.startswith("["):
        body = raw[raw.index("[", 1) + 1: raw.rindex("]", 0, len(raw) - 1)]
        return body[1:] if body.startswith("\n") else body
    out, i, body = [], 0, raw[1:-1]
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class _Parser:
    def __init__(self, text: str):
        self.toks = list(_tokens(text))
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def take(self, kind=None, value=None):
        tok = self.toks[self.i]
        if (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            want = value or kind
            raise ConfigError(f"config.lua line {tok[2]}: expected {want!r} but found {tok[1] or 'end of file'!r}")
        self.i += 1
        return tok

    def value(self):
        kind, raw, line = self.peek()
        if kind in ("string", "lstring"):
            self.i += 1
            return _unquote(raw)
        if kind == "number":
            self.i += 1
            return float(raw) if "." in raw else int(raw)
        if kind == "name" and raw in ("true", "false"):
            self.i += 1
            return raw == "true"
        if kind == "name" and raw == "nil":
            self.i += 1
            return None
        if raw == "{":
            return self.table()
        raise ConfigError(f"config.lua line {line}: expected a value but found {raw!r}")

    def table(self):
        self.take("punct", "{")
        items, fields = [], {}
        while self.peek()[1] != "}":
            kind, raw, line = self.peek()
            nxt = self.toks[self.i + 1]
            if kind == "name" and nxt[1] == "=":
                self.i += 2
                fields[raw] = self.value()
            elif raw == "[":
                self.i += 1
                key = self.value()
                self.take("punct", "]")
                self.take("punct", "=")
                fields[str(key)] = self.value()
            else:
                items.append(self.value())
            if self.peek()[1] in (",", ";"):
                self.i += 1
            elif self.peek()[1] != "}":
                raise ConfigError(f"config.lua line {self.peek()[2]}: missing ',' before {self.peek()[1]!r}")
        self.take("punct", "}")
        if items and fields:
            raise ConfigError(f"config.lua line {line}: a table cannot mix a list and named settings")
        return fields if fields or not items else items

    def document(self) -> dict:
        root: dict = {}
        while self.peek()[0] != "eof":
            if self.peek()[1] == ";":
                self.i += 1
                continue
            _, name, line = self.take("name")
            if name == "local":
                _, name, line = self.take("name")
            path = [name]
            while self.peek()[1] == ".":
                self.i += 1
                path.append(self.take("name")[1])
            self.take("punct", "=")
            value = self.value()
            if path[0] != "Config":
                raise ConfigError(f"config.lua line {line}: only 'Config...' settings are allowed (found {path[0]})")
            path = path[1:]
            if not path:
                if value not in ({}, []):
                    raise ConfigError(f"config.lua line {line}: use 'Config = {{}}' then 'Config.section = {{...}}'")
                continue
            target = root
            for part in path[:-1]:
                target = target.setdefault(part, {})
                if not isinstance(target, dict):
                    raise ConfigError(f"config.lua line {line}: Config.{'.'.join(path)} is not a section")
            if isinstance(target.get(path[-1]), dict) and isinstance(value, dict):
                target[path[-1]].update(value)
            else:
                target[path[-1]] = value
        return root


_LIST_HINT = {"databases", "exclude_databases", "exclude_tables", "schema_only_tables", "tables", "daily_times",
              "process_names", "command_line_contains"}


def loads(text: str) -> dict:
    data = _Parser(text).document()
    for section in data.values():
        if isinstance(section, dict):
            for key, value in section.items():
                if value == {} and key in _LIST_HINT:
                    section[key] = []
    return data


def _lua_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "{ " + ", ".join(_lua_value(v) for v in value) + " }" if value else "{}"
    s = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def render(spec: dict, values: dict, header: str) -> str:
    """One setting per line: key = value, -- explanation"""
    out = [f"-- {line}" for line in header.splitlines() if line] + ["Config = {}"]
    if len(out) > 1:
        out.insert(-1, "")
    for section, keys in spec.items():
        out += ["", f"Config.{section} = {{"]
        for key, (_kind, default, doc) in keys.items():
            out.append(f"    {key} = {_lua_value(values.get(section, {}).get(key, default))}, -- {doc}")
        out.append("}")
    return "\n".join(out) + "\n"
