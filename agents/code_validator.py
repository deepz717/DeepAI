"""
Code validator — strict multi-stage checks.
===========================================
Design:
  * When a real compiler is available, its verdict is the source of
    truth. If it says PASS, we do NOT run our heuristic on top
    (heuristics produce false positives on valid C).
  * When no compiler is present, we fall back to structural checks
    that are deliberately conservative — only flag clear problems.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────
# Block extraction
# ─────────────────────────────────────────────────────────────
def _extract_blocks(answer: str) -> List[Tuple[str, str]]:
    return [
        (lang.strip().lower(), body)
        for lang, body in re.findall(
            r"```([a-zA-Z0-9_+\-]*)\n(.*?)```", answer, re.DOTALL
        )
    ]


def _explanation_after_blocks(answer: str) -> str:
    idx = answer.rfind("```")
    if idx < 0:
        return ""
    return answer[idx + 3:].strip().lower()


_LANG_ALIASES = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "java": "java",
    "c": "c", "c++": "cpp", "cpp": "cpp",
    "c#": "csharp", "csharp": "csharp",
    "go": "go", "golang": "go",
    "rust": "rust", "rs": "rust",
}


def _normalize_lang(lang: str) -> str:
    return _LANG_ALIASES.get(lang.strip().lower(), lang.strip().lower())


_COMPILER_CACHE: Dict[str, Optional[str]] = {}


def _compiler_path(lang: str) -> Optional[str]:
    if lang in _COMPILER_CACHE:
        return _COMPILER_CACHE[lang]
    candidates = {
        "python":     ["python", "python3"],
        "java":       ["javac"],
        "c":          ["gcc", "clang"],
        "cpp":        ["g++", "clang++"],
        "javascript": ["node"],
        "typescript": ["npx"],
        "go":         ["go"],
        "rust":       ["rustc"],
    }.get(lang, [])
    for c in candidates:
        p = shutil.which(c)
        if p:
            _COMPILER_CACHE[lang] = p
            return p
    _COMPILER_CACHE[lang] = None
    return None


# ─────────────────────────────────────────────────────────────
# Stage: Language
# ─────────────────────────────────────────────────────────────
def validate_language(answer: str,
                      expected_lang: Optional[str]) -> Tuple[str, str]:
    if not expected_lang:
        return ("SKIP", "")
    blocks = _extract_blocks(answer)
    if not blocks:
        return ("FAIL", "No fenced code block present.")
    expected = _normalize_lang(expected_lang)
    for lang, body in blocks:
        norm = _normalize_lang(lang)
        if norm and norm != expected:
            return ("FAIL", f"Block is {norm}, expected {expected}.")
        if _fingerprint(body, expected):
            return ("PASS", "")
    return ("FAIL", f"Code does not look like {expected}.")


_FINGERPRINTS = {
    "python": [
        r"^\s*def\s+\w+\s*\(", r"^\s*import\s+\w+",
        r"^\s*from\s+\w+\s+import", r"\bprint\s*\(",
    ],
    "c": [r"#include\s*<", r"\bint\s+main\s*\(", r"\bprintf\s*\("],
    "cpp": [r"#include\s*<", r"\bstd::", r"\busing\s+namespace\s+std"],
    "csharp": [r"\busing\s+System", r"\bConsole\.WriteLine",
                r"\bnamespace\s+\w+"],
    "java": [r"\b(public\s+)?class\s+\w+", r"\bpublic\s+static",
              r"\bSystem\.out\.print", r"\bimport\s+java\."],
    "javascript": [r"\bfunction\s+\w+", r"\bconst\s+\w+\s*=",
                    r"\blet\s+\w+\s*=", r"console\.log"],
    "typescript": [r":\s*(number|string|boolean)\b",
                    r"\binterface\s+\w+"],
    "go": [r"^package\s+main", r"\bfunc\s+\w+\s*\("],
    "rust": [r"\bfn\s+\w+\s*\(", r"\blet\s+mut\b"],
}


def _fingerprint(body: str, lang: str) -> bool:
    pats = _FINGERPRINTS.get(lang)
    if not pats:
        return True
    return any(re.search(p, body, re.MULTILINE) for p in pats)


# ─────────────────────────────────────────────────────────────
# Stage: Syntax
# ─────────────────────────────────────────────────────────────
def validate_syntax(answer: str) -> Tuple[str, str]:
    blocks = _extract_blocks(answer)
    if not blocks:
        return ("FAIL", "No fenced code block.")
    for lang, body in blocks:
        norm = _normalize_lang(lang) or "python"
        v, r = _syntax_check(body, norm)
        if v == "FAIL":
            return (v, r)
    return ("PASS", "")


def _syntax_check(body: str, lang: str) -> Tuple[str, str]:
    """
    Run the strictest check available.

    Priority:
      1. Real compiler (if installed) — always trusted.
         Compiler PASS short-circuits; do not run structural
         heuristics on top.
      2. Structural fallback — only when no compiler.
    """
    compiler = _compiler_path(lang)
    if compiler:
        v, r = _compile_with(body, lang, compiler)
        if v == "PASS":
            return ("PASS", "")
        if v == "FAIL":
            return (v, r)
        # SKIP: fall through

    if lang == "python":
        return _py_syntax(body)
    if lang == "java":
        return _java_syntax(body)
    if lang in ("c", "cpp"):
        return _c_syntax(body, lang)
    if lang in ("javascript", "typescript"):
        return _js_syntax(body)
    return ("PASS", "")


def _compile_with(body: str, lang: str, compiler: str) -> Tuple[str, str]:
    try:
        if lang == "python":
            try:
                ast.parse(body)
                return ("PASS", "")
            except SyntaxError as e:
                return ("FAIL",
                        f"Python syntax error line {e.lineno}: {e.msg}")
        if lang == "java":
            with tempfile.TemporaryDirectory() as d:
                m = re.search(r"\bpublic\s+class\s+(\w+)", body)
                if not m:
                    return _java_syntax(body)
                path = os.path.join(d, m.group(1) + ".java")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                r = subprocess.run(
                    [compiler, "-d", d, path],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode != 0:
                    msg = (r.stderr or r.stdout or "").strip()
                    first = msg.split("\n")[0][:200]
                    return ("FAIL", f"javac: {first}")
                return ("PASS", "")
        if lang in ("c", "cpp"):
            ext = ".c" if lang == "c" else ".cpp"
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "main" + ext)
                with open(src, "w", encoding="utf-8") as fh:
                    fh.write(body)
                r = subprocess.run(
                    [compiler, "-fsyntax-only", "-Wall", src],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode != 0:
                    msg = (r.stderr or r.stdout or "").strip()
                    first = msg.split("\n")[0][:200]
                    for line in msg.split("\n"):
                        if "error:" in line:
                            first = line.strip()[:200]
                            break
                    return ("FAIL", f"{lang}: {first}")
                return ("PASS", "")
        if lang in ("javascript", "typescript"):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "code.js")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                r = subprocess.run(
                    [compiler, "--check", path],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode != 0:
                    msg = (r.stderr or "").strip().split("\n")[0][:200]
                    return ("FAIL", f"node --check: {msg}")
                return ("PASS", "")
        return ("SKIP", "")
    except Exception as e:
        return ("SKIP", f"Compile attempt errored: {e}")


def _py_syntax(body: str) -> Tuple[str, str]:
    try:
        ast.parse(body)
        return ("PASS", "")
    except SyntaxError as e:
        return ("FAIL", f"Python syntax error line {e.lineno}: {e.msg}")


def _java_syntax(body: str) -> Tuple[str, str]:
    if body.count("{") != body.count("}"):
        return ("FAIL", "Unbalanced braces in Java.")
    if body.count("(") != body.count(")"):
        return ("FAIL", "Unbalanced parentheses in Java.")
    classes = re.findall(r"\bclass\s+(\w+)", body)
    if classes:
        primary = classes[0]
        for ident in set(re.findall(r"\b([A-Z]\w+)\b", body)):
            if ident == primary:
                continue
            if ident.lower() == primary.lower():
                return ("FAIL",
                        f"Class mismatch: '{ident}' vs '{primary}'.")
            if _edit_distance(ident.lower(), primary.lower()) <= 1 \
               and abs(len(ident) - len(primary)) <= 1:
                return ("FAIL",
                        f"Identifier typo: '{ident}' ≠ '{primary}'.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# C/C++ reserved identifiers
# ─────────────────────────────────────────────────────────────
_C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default",
    "do", "double", "else", "enum", "extern", "float", "for", "goto",
    "if", "inline", "int", "long", "register", "restrict", "return",
    "short", "signed", "sizeof", "static", "struct", "switch",
    "typedef", "union", "unsigned", "void", "volatile", "while",
    "_Bool", "_Complex", "_Imaginary",
    "printf", "scanf", "fprintf", "sprintf", "snprintf",
    "malloc", "calloc", "realloc", "free", "exit", "abort",
    "fopen", "fclose", "fread", "fwrite", "fgets", "fputs",
    "strlen", "strcpy", "strcmp", "strcat", "memset", "memcpy",
    "pow", "sqrt", "floor", "ceil", "fabs", "log", "exp",
    "sin", "cos", "tan", "fmod",
    "atoi", "atof", "atol", "strtol", "strtod",
    "isdigit", "isalpha", "isalnum", "toupper", "tolower",
    "NULL", "EOF", "stdin", "stdout", "stderr",
    "true", "false", "bool",
    "main",
    "MAX", "MIN", "PI", "TRUE", "FALSE",
}

_C_STDLIB_FNS = {
    "printf", "scanf", "fprintf", "sprintf", "snprintf",
    "malloc", "calloc", "realloc", "free", "exit", "abort",
    "fopen", "fclose", "fread", "fwrite", "fgets", "fputs",
    "strlen", "strcpy", "strcmp", "strcat", "memset", "memcpy",
    "pow", "sqrt", "floor", "ceil", "fabs", "log", "exp",
    "sin", "cos", "tan", "fmod", "abs", "labs",
    "atoi", "atof", "atol", "strtol", "strtod",
    "isdigit", "isalpha", "isalnum", "toupper", "tolower",
    "qsort", "rand", "srand", "strstr", "strchr",
    "strncpy", "strncmp",
}


def _detect_undeclared_c_identifiers(body: str) -> List[str]:
    """
    Very conservative fallback heuristic. Only flags identifiers
    that look like a typo of a declared identifier (Levenshtein
    distance ≤ 2) or a bare single-letter name used outside a
    for-header.

    This is only used when no real compiler is available. The
    compiler is always preferred because it doesn't produce false
    positives on valid C.
    """
    clean = re.sub(r'"(?:[^"\\]|\\.)*"', '""', body)
    clean = re.sub(r"'(?:[^'\\]|\\.)*'", "''", clean)
    clean = re.sub(r"//[^\n]*", "", clean)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)

    declared = set()

    decl_pat = re.compile(
        r"\b(?:const\s+|static\s+|unsigned\s+|signed\s+|short\s+|"
        r"long\s+|struct\s+\w+\s+)?"
        r"(?:int|char|float|double|void|long|short|size_t|"
        r"uint8_t|uint16_t|uint32_t|uint64_t|"
        r"int8_t|int16_t|int32_t|int64_t|FILE|time_t)"
        r"\s*\*?\s*"
        r"([A-Za-z_]\w*)"
    )
    for m in decl_pat.finditer(clean):
        declared.add(m.group(1))

    multi_pat = re.compile(
        r"\b(?:int|char|float|double|long|short|size_t)\s+"
        r"([A-Za-z_]\w*(?:\s*,\s*\*?\s*[A-Za-z_]\w*)+)\s*[;=]"
    )
    for m in multi_pat.finditer(clean):
        for name in re.findall(r"[A-Za-z_]\w*", m.group(1)):
            declared.add(name)

    func_pat = re.compile(
        r"\b[A-Za-z_]\w*\s+\*?\s*([A-Za-z_]\w*)\s*\("
    )
    for m in func_pat.finditer(clean):
        declared.add(m.group(1))

    for m in re.finditer(
        r"\bfor\s*\(\s*(?:int|long|unsigned|size_t|short)\s+"
        r"([A-Za-z_]\w*)",
        clean,
    ):
        declared.add(m.group(1))

    for m in re.finditer(r"\btypedef\b.*?\b([A-Za-z_]\w*)\s*;", clean):
        declared.add(m.group(1))

    declared |= _C_KEYWORDS
    declared |= _C_STDLIB_FNS

    clean_no_directives = re.sub(
        r"^\s*#.*$", "", clean, flags=re.MULTILINE
    )

    used_pat = re.compile(r"\b([a-z_]\w*)\b")
    suspicious = []
    for m in used_pat.finditer(clean_no_directives):
        name = m.group(1)
        if name in declared:
            continue

        # Only flag single-letter names used outside a for-header
        if len(name) == 1:
            line_start = clean_no_directives.rfind("\n", 0, m.start()) + 1
            line_end = clean_no_directives.find("\n", m.end())
            if line_end == -1:
                line_end = len(clean_no_directives)
            line = clean_no_directives[line_start:line_end]
            if "for" in line and "(" in line and ")" in line:
                continue
            suspicious.append(name)
            continue

        # Only flag longer names if they look like a typo of a
        # declared identifier (edit distance 1 or 2)
        close = [d for d in declared
                 if 0 < _edit_distance(name, d) <= 2]
        if close:
            suspicious.append(name)

    seen = set()
    result = []
    for name in suspicious:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= 5:
            break
    return result


def _c_syntax(body: str, lang: str) -> Tuple[str, str]:
    if body.count("{") != body.count("}"):
        return ("FAIL", f"Unbalanced braces in {lang}.")
    if body.count("(") != body.count(")"):
        return ("FAIL", f"Unbalanced parentheses in {lang}.")

    first_include = body.find("#include")
    first_decl = re.search(
        r"^\s*(int|void|char|float|double|struct|long|unsigned)\b",
        body, re.MULTILINE,
    )
    if first_include >= 0 and first_decl and \
       first_include > first_decl.start():
        return ("FAIL", "#include must precede declarations.")

    # Missing includes for standard library functions
    if re.search(
        r"\b(pow|sqrt|floor|ceil|fabs|log|exp|sin|cos|tan|fmod)\s*\(",
        body,
    ):
        if "#include <math.h>" not in body and \
           '#include "math.h"' not in body:
            return ("FAIL",
                    "math.h not included but math functions are used.")
    if re.search(
        r"\b(abs|malloc|calloc|realloc|free|exit|atoi|atof|atol|"
        r"qsort|rand|srand)\s*\(",
        body,
    ):
        if "#include <stdlib.h>" not in body and \
           '#include "stdlib.h"' not in body:
            return ("FAIL",
                    "stdlib.h not included but stdlib functions "
                    "are used.")
    if re.search(
        r"\b(strcpy|strcmp|strcat|strlen|strncpy|strncmp|memcpy|"
        r"memset|strstr|strchr)\s*\(",
        body,
    ):
        if "#include <string.h>" not in body and \
           '#include "string.h"' not in body:
            return ("FAIL",
                    "string.h not included but string functions "
                    "are used.")
    if re.search(
        r"\b(isalpha|isdigit|isalnum|isspace|toupper|tolower)\s*\(",
        body,
    ):
        if "#include <ctype.h>" not in body and \
           '#include "ctype.h"' not in body:
            return ("FAIL",
                    "ctype.h not included but ctype functions "
                    "are used.")

    # main return type
    if re.search(r"\bvoid\s+main\s*\(", body):
        return ("FAIL",
                "main() must have return type 'int', not 'void'.")

    # Function used before declaration
    fn_pattern = re.compile(
        r"^\s*(?:static\s+|inline\s+)?"
        r"(?:int|void|char|float|double|long|short|"
        r"unsigned|signed|size_t|"
        r"[A-Za-z_]\w*)"
        r"(?:\s*\*+)?\s+"
        r"([A-Za-z_]\w*)\s*\(",
        re.MULTILINE,
    )
    definitions = [(m.group(1), m.start())
                   for m in fn_pattern.finditer(body)]

    call_pattern = re.compile(r"\b([a-z_]\w*)\s*\(")
    for m in call_pattern.finditer(body):
        name = m.group(1)
        if name in _C_STDLIB_FNS:
            continue
        if name in ("if", "while", "for", "switch", "sizeof",
                     "return", "main"):
            continue
        before = [pos for n, pos in definitions
                  if n == name and pos < m.start()]
        after = [pos for n, pos in definitions
                 if n == name and pos > m.start()]
        if not before and after:
            return ("FAIL",
                    f"Function '{name}' is used before its "
                    f"definition. Add a function prototype or "
                    f"move the definition above its use.")

    # Very conservative undeclared-identifier check
    undeclared = _detect_undeclared_c_identifiers(body)
    if undeclared:
        return ("FAIL",
                f"Undeclared identifier(s): {', '.join(undeclared)}.")

    return ("PASS", "")


def _js_syntax(body: str) -> Tuple[str, str]:
    if body.count("{") != body.count("}"):
        return ("FAIL", "Unbalanced braces in JavaScript.")
    if body.count("(") != body.count(")"):
        return ("FAIL", "Unbalanced parentheses.")
    if body.count("[") != body.count("]"):
        return ("FAIL", "Unbalanced brackets.")
    return ("PASS", "")


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1, cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


# ─────────────────────────────────────────────────────────────
# Stage: Type Safety
# ─────────────────────────────────────────────────────────────
def validate_type_safety(answer: str) -> Tuple[str, str]:
    blocks = _extract_blocks(answer)
    for lang, body in blocks:
        norm = _normalize_lang(lang)
        if norm == "java":
            if re.search(r"\bint\s*\[\s*\]\s*\w+\s*=", body):
                for m in re.finditer(
                    r"\bint\s*\[\s*\]\s+(\w+)\s*=", body
                ):
                    name = m.group(1)
                    rhs = body[m.end():].split(";", 1)[0]
                    if re.search(r"\bnew\s+(?!int\s*\[)", rhs):
                        return ("FAIL",
                                f"Cannot assign non-int[] to int[] "
                                f"'{name}'.")
        if norm in ("c", "cpp"):
            if re.search(r"\bint\s*\*\s*\w+\s*\(", body) and \
               re.search(r"return\s+&[^;]*,[^;]*;", body):
                return ("FAIL",
                        "int* function returns comma-separated "
                        "values — only the last operand is "
                        "returned.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Stage: Compilation
# ─────────────────────────────────────────────────────────────
def validate_compilation(answer: str) -> Tuple[str, str]:
    blocks = _extract_blocks(answer)
    for lang, body in blocks:
        norm = _normalize_lang(lang)
        if norm == "java":
            imported = set(re.findall(
                r"import\s+[\w.]+\.(\w+)\s*;", body
            ))
            for cls in ("Arrays", "HashMap", "ArrayList", "List",
                        "Map", "Scanner", "Collections", "Set",
                        "HashSet"):
                if re.search(rf"\b{cls}\b", body) and cls not in imported:
                    return ("FAIL",
                            f"'{cls}' used but not imported.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Stage: Logic
# ─────────────────────────────────────────────────────────────
def validate_logic(answer: str,
                   topic: Optional[str]) -> Tuple[str, str]:
    if not topic:
        return ("SKIP", "")
    t = topic.lower()
    blocks = _extract_blocks(answer)
    if not blocks:
        return ("FAIL", "No code to check.")
    lang, body = blocks[0]
    lang = _normalize_lang(lang) or "python"
    code = "\n".join(b for _, b in blocks).lower()

    if "two sum" in t or "two-sum" in t:
        return _logic_two_sum(code, body, lang)
    if "palindrome" in t:
        return _logic_palindrome(code, body, lang)
    if "binary search" in t or "binary-search" in t:
        return _logic_binary_search(code)
    if "armstrong" in t:
        return _logic_armstrong(code, body)
    if "prime" in t:
        return _logic_prime(code)
    if "factorial" in t:
        return _logic_factorial(code)
    if "fibonacci" in t:
        return _logic_fibonacci(code)
    if "reverse" in t and "integer" in t:
        return _logic_reverse_integer(code)
    if "gcd" in t or "greatest common" in t:
        return _logic_gcd(code)
    if "digit" in t and "sum" in t:
        return _logic_digit_sum(code)
    if "count" in t and "digit" in t:
        return _logic_count_digits(code)
    if "perfect number" in t:
        return _logic_perfect(code)
    return ("SKIP", "")


def _logic_two_sum(code: str, body: str,
                    lang: str) -> Tuple[str, str]:
    if lang == "java":
        for m in re.finditer(
            r"containsKey\s*\(\s*([^)]+?)\s*\)\s*\{.*?"
            r"get\s*\(\s*([^)]+?)\s*\)",
            body, re.DOTALL,
        ):
            check = re.sub(r"\s+", "", m.group(1))
            get = re.sub(r"\s+", "", m.group(2))
            if check != get:
                return ("FAIL",
                        f"containsKey({m.group(1).strip()}) "
                        f"checked but get({m.group(2).strip()}) "
                        f"retrieved.")
    if re.search(r"return\s+&?\w+\s*,\s*&?\w+\s*;", code):
        return ("FAIL",
                "Comma operator returns one value, not two.")
    has_data = any(kw in code for kw in (
        "hashmap", "dict", "seen", "map<", "map.put", "map.get",
        "map[", "numtoindex", "complement", "{}", "num_indices",
    ))
    has_nested = bool(re.search(r"for\s+.*:\s*\n\s+.*for", code)) \
                 or bool(re.search(
                     r"for\s*\(.*\)\s*\{[^}]*for\s*\(", code))
    has_sort = "sort" in code
    has_pair_check = ("for" in code and "+" in code and "==" in code)
    if has_data or has_nested or has_sort or has_pair_check:
        return ("PASS", "")
    return ("FAIL",
            "Two-sum algorithm does not use any recognized pattern.")


def _logic_palindrome(code: str, body: str,
                       lang: str) -> Tuple[str, str]:
    if lang == "java":
        classes = re.findall(r"\bclass\s+(\w+)", body)
        if classes:
            primary = classes[0]
            for m in re.finditer(r"\b([A-Z]\w+)\.\w+\s*\(", body):
                called = m.group(1)
                if called != primary and \
                   _edit_distance(called.lower(),
                                   primary.lower()) <= 1:
                    return ("FAIL",
                            f"Class mismatch: '{called}' ≠ "
                            f"'{primary}'.")
    if not ("reverse" in code or "[::-1]" in code
            or "charat" in code or "left" in code
            or "start" in code or "end" in code
            or "reversed" in code):
        return ("FAIL", "Palindrome check does not compare ends.")
    return ("PASS", "")


def _logic_binary_search(code: str) -> Tuple[str, str]:
    if "mid" not in code and "middle" not in code:
        return ("FAIL", "Binary search has no midpoint.")
    return ("PASS", "")


def _logic_armstrong(code: str, body: str) -> Tuple[str, str]:
    counts_digits = bool(re.search(
        r"while\s*\(\s*\w+\s*!=\s*0\s*\)[^{]*\{[^}]*\w+\s*\+\+",
        code,
    )) or bool(re.search(r"len\s*\(\s*str\s*\(", code)) \
       or bool(re.search(r"\bn\s*=\s*0\b", code)) \
       or bool(re.search(r"num_str\s*=\s*str", code)) \
       or bool(re.search(r"\bk\s*=\s*0\b", code))
    sums_digits = bool(re.search(
        r"while\s*\(\s*\w+\s*!=\s*0\s*\)[^{]*\{[^}]*%",
        code,
    )) or bool(re.search(r"for\s+\w+\s+in\s+\w+", code)) \
       or bool(re.search(
           r"for\s*\([^)]*;[^)]*;[^)]*\)[^{]*\{[^}]*%", code))
    compares = bool(re.search(r"==\s*\w+", code)) or \
               bool(re.search(r"\w+\s*==", code))
    if counts_digits and sums_digits and compares:
        return ("PASS", "")
    missing = []
    if not counts_digits:
        missing.append("digit count")
    if not sums_digits:
        missing.append("digit sum")
    if not compares:
        missing.append("comparison")
    return ("FAIL",
            f"Armstrong check missing: {', '.join(missing)}.")


def _logic_prime(code: str) -> Tuple[str, str]:
    if "for" not in code or "%" not in code:
        return ("FAIL", "Prime check has no loop or modulo test.")
    return ("PASS", "")


def _logic_factorial(code: str) -> Tuple[str, str]:
    if "factorial" not in code:
        return ("FAIL", "No factorial function.")
    if "return" not in code:
        return ("FAIL", "Factorial has no return statement.")
    return ("PASS", "")


def _logic_fibonacci(code: str) -> Tuple[str, str]:
    if "fib" not in code and "fibonacci" not in code:
        return ("FAIL", "No Fibonacci computation.")
    return ("PASS", "")


def _logic_reverse_integer(code: str) -> Tuple[str, str]:
    if not ("reverse" in code or "reversed" in code):
        return ("FAIL", "No reverse accumulator.")
    if "%" not in code or "/" not in code:
        return ("FAIL", "Reverse must use % 10 and / 10.")
    return ("PASS", "")


def _logic_gcd(code: str) -> Tuple[str, str]:
    if "while" not in code or "%" not in code:
        return ("FAIL", "GCD has no Euclidean loop.")
    return ("PASS", "")


def _logic_digit_sum(code: str) -> Tuple[str, str]:
    if "%" not in code or "/" not in code:
        return ("FAIL", "Digit sum has no % / loop.")
    return ("PASS", "")


def _logic_count_digits(code: str) -> Tuple[str, str]:
    if "while" not in code or "/" not in code:
        return ("FAIL", "Digit count has no division loop.")
    return ("PASS", "")


def _logic_perfect(code: str) -> Tuple[str, str]:
    if "for" not in code or "%" not in code:
        return ("FAIL",
                "Perfect-number check has no loop or modulo.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Stage: Runtime
# ─────────────────────────────────────────────────────────────
def validate_runtime(answer: str) -> Tuple[str, str]:
    blocks = _extract_blocks(answer)
    for lang, body in blocks:
        norm = _normalize_lang(lang)
        if norm == "java":
            for m in re.finditer(
                r"System\.out\.print(?:ln)?\s*\(\s*(\w+)\s*\)\s*;",
                body,
            ):
                var = m.group(1)
                if re.search(
                    rf"\bint\s*\[\s*\]\s+{re.escape(var)}\s*=", body
                ):
                    if not re.search(
                        rf"Arrays\.toString\s*\(\s*"
                        rf"{re.escape(var)}",
                        body,
                    ):
                        return ("FAIL",
                                f"System.out.println({var}) prints "
                                f"the array identity, not its "
                                f"contents.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Stage: Explanation
# ─────────────────────────────────────────────────────────────
def validate_explanation(answer: str) -> Tuple[str, str]:
    expl = _explanation_after_blocks(answer)
    if not expl:
        return ("SKIP", "")
    code = "\n".join(body for _, body
                     in _extract_blocks(answer)).lower()

    if "in operator" in expl:
        if not (" in seen" in code or " in map" in code
                or " in num" in code or " in arr" in code
                or " in nums" in code):
            return ("FAIL",
                    "Explanation claims 'in operator' but code "
                    "does not use it.")
    if "uses an array" in expl or "using an array" in expl:
        if ("hashmap" in code or "dictionary" in code
                or "dict" in code or "map<" in code):
            return ("FAIL",
                    "Explanation claims an array but code uses a "
                    "hash map.")
    if "hashmap" in expl or "hash map" in expl:
        if not ("hashmap" in code or "dict" in code
                or "map<" in code or "map.put" in code):
            return ("FAIL",
                    "Explanation claims a hash map but code "
                    "doesn't use one.")
    if ("replaced" in expl and "hashmap" in expl and
            "hashset" in expl):
        if "hashmap" in code:
            return ("FAIL",
                    "Explanation claims HashMap was replaced by "
                    "HashSet but code still uses HashMap.")
    if "o(1)" in expl.replace(" ", "") and "space" in expl:
        if ("hashmap" in code or "dict" in code
                or "new int[" in code or "list<" in code):
            return ("FAIL",
                    "Explanation claims O(1) space but code "
                    "allocates a per-input structure.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Stage: Complexity
# ─────────────────────────────────────────────────────────────
def validate_complexity(answer: str) -> Tuple[str, str]:
    expl = _explanation_after_blocks(answer)
    if not expl:
        return ("SKIP", "")
    code = "\n".join(body for _, body
                     in _extract_blocks(answer)).lower()
    ec = expl.replace(" ", "").replace("^", "")
    nested = bool(re.search(r"for\s+.*:\s*\n\s+.*for", code)) \
             or bool(re.search(r"for\s*\(.*\)\s*\{[^}]*for", code))
    uses_map = ("hashmap" in code or "dict" in code
                or "map.put" in code or "map.get" in code
                or "numtoindex" in code)
    if "o(n)" in ec and "o(n2)" not in ec and nested:
        return ("FAIL", "Claims O(n) but code has nested loops.")
    if "o(1)" in ec and "space" in expl and uses_map:
        return ("FAIL",
                "Claims O(1) space but code grows a hash map.")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Stage: Examples
# ─────────────────────────────────────────────────────────────
def validate_examples(answer: str,
                      topic: Optional[str] = None) -> Tuple[str, str]:
    if not topic:
        return ("SKIP", "")
    t = topic.lower()
    blocks = _extract_blocks(answer)
    if not blocks:
        return ("SKIP", "")
    lang, body = blocks[0]
    lang = _normalize_lang(lang) or "python"

    if "two sum" in t or "two-sum" in t:
        m = re.search(
            r"\[\s*2\s*,\s*7\s*,\s*11\s*,\s*15\s*\]\s*,\s*9",
            answer,
        )
        if m:
            tail = answer[m.start():]
            out_match = re.search(
                r"(?:output|returns?|=|→|->|:)\s*"
                r"[\(\[]\s*(\d+)\s*,\s*(\d+)\s*[\)\]]",
                tail, re.IGNORECASE,
            )
            if out_match:
                claimed = (int(out_match.group(1)),
                            int(out_match.group(2)))
                if set(claimed) != {0, 1}:
                    return ("FAIL",
                            f"Two-sum example claims output "
                            f"{claimed} but should be [0, 1].")
    return ("PASS", "")


# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────
def validate_code_answer(answer: str,
                         topic: Optional[str],
                         target_language: Optional[str],
                         ) -> Dict[str, str]:
    out: Dict[str, str] = {
        "language":     "SKIP",
        "syntax":       "SKIP",
        "type_safety":  "SKIP",
        "compilation":  "SKIP",
        "logic":        "SKIP",
        "runtime":      "SKIP",
        "explanation":  "SKIP",
        "complexity":   "SKIP",
        "examples":     "SKIP",
        "reason":       "",
    }
    failures: List[str] = []

    for name, fn in (
        ("language",    lambda: validate_language(answer, target_language)),
        ("syntax",      lambda: validate_syntax(answer)),
        ("type_safety", lambda: validate_type_safety(answer)),
        ("compilation", lambda: validate_compilation(answer)),
        ("logic",       lambda: validate_logic(answer, topic)),
        ("runtime",     lambda: validate_runtime(answer)),
        ("explanation", lambda: validate_explanation(answer)),
        ("complexity",  lambda: validate_complexity(answer)),
        ("examples",    lambda: validate_examples(answer, topic)),
    ):
        try:
            v, r = fn()
        except Exception as e:
            v, r = "SKIP", f"{name} errored: {e}"
        out[name] = v
        if v == "FAIL" and r:
            failures.append(r)

    out["reason"] = "; ".join(failures)
    return out