"""
Optimization validator — detects false optimization claims and
identifies when no optimization is needed.
================================================================
Verdicts:
  PASS          — genuine asymptotic improvement demonstrated
  NOT_NEEDED    — current solution is already asymptotically optimal
  FAIL          — false claim, regression, or mismatch

Also detects the specific Java key/retrieval mismatch bug:
    if (map.containsKey(complement)) {
        return new int[]{map.get(other_key), i};   // ← wrong
    }
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────
# Extract code block
# ─────────────────────────────────────────────────────────────
def _extract_first_block(text: str) -> str:
    m = re.search(r"```[a-zA-Z0-9_+\-]*\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else ""


def _extract_explanation(text: str) -> str:
    idx = text.rfind("```")
    if idx < 0:
        return text.strip().lower()
    return text[idx + 3:].strip().lower()


# ─────────────────────────────────────────────────────────────
# Structural markers
# ─────────────────────────────────────────────────────────────
_MARKERS = {
    "hash_map": [
        "hashmap", "hash_map", "dict", "numtoindex", "num_to_index",
        "map.put", "map.get", "map<", "map[", " map ", "seen",
        "complement", "num_to_index", "num_indices",
    ],
    "hash_set": ["hashset", "set<", "new set"],
    "array": ["int[]", "int []", "vector<", "list<", "[]int"],
    "sort": ["sort", "sorted", "qsort", "arrays.sort"],
    "recursion": ["recursion", "recursive"],
    "two_pointer": ["left", "right", "two pointer", "two-pointer"],
}


def _detect_markers(code: str) -> Dict[str, bool]:
    low = code.lower()
    out: Dict[str, bool] = {}
    for key, kws in _MARKERS.items():
        out[key] = any(kw in low for kw in kws)
    out["nested_loop"] = bool(re.search(
        r"for\s+.*:\s*\n\s+.*for", code
    )) or bool(re.search(
        r"for\s*\(.*\)\s*\{[^}]*for\s*\(", code
    )) or bool(re.search(
        r"while\s*\(.*\)\s*\{[^}]*while\s*\(", code
    ))
    return out


# ─────────────────────────────────────────────────────────────
# Complexity estimation
# ─────────────────────────────────────────────────────────────
def _estimate_complexity(code: str, markers: Dict[str, bool]
                         ) -> Tuple[str, str]:
    low = code.lower()
    if markers["nested_loop"]:
        time_c = "O(n^2)"
    elif markers["sort"] and (markers["two_pointer"] or "for" in low):
        time_c = "O(n log n)"
    elif markers["hash_map"] and "for" in low:
        time_c = "O(n)"
    elif markers["hash_set"] and "for" in low:
        time_c = "O(n)"
    elif "for" in low or "while" in low:
        time_c = "O(n)"
    elif markers["recursion"]:
        time_c = "O(2^n)"
    else:
        time_c = "O(1)"

    if markers["hash_map"] or markers["hash_set"]:
        space_c = "O(n)"
    elif markers["array"] and "new " in low:
        space_c = "O(n)"
    elif markers["recursion"]:
        space_c = "O(n)"
    else:
        space_c = "O(1)"
    return time_c, space_c


def _describe_algorithm(markers: Dict[str, bool]) -> str:
    if markers["nested_loop"]:
        return "brute-force (nested loops)"
    if markers["sort"] and markers["two_pointer"]:
        return "sort + two-pointer"
    if markers["hash_map"]:
        return "hash map"
    if markers["hash_set"]:
        return "hash set"
    if markers["two_pointer"]:
        return "two-pointer"
    return "direct"


# ─────────────────────────────────────────────────────────────
# Complexity ranks
# ─────────────────────────────────────────────────────────────
_RANK = {
    "o(1)": 0, "o(logn)": 1, "o(n)": 2,
    "o(nlogn)": 3, "o(n^2)": 4, "o(n²)": 4, "o(n2)": 4,
    "o(2^n)": 5,
}


def _rank(cx: str) -> int:
    return _RANK.get(cx.replace(" ", "").lower(), -1)


def _is_improvement(before: str, after: str) -> bool:
    rb, ra = _rank(before), _rank(after)
    if rb < 0 or ra < 0:
        return False
    return ra < rb


# ─────────────────────────────────────────────────────────────
# Detect key/retrieval mismatch (Java two-sum bug class)
# ─────────────────────────────────────────────────────────────
def _detect_key_mismatch(code: str) -> Optional[str]:
    """
    Find patterns like:
        if (map.containsKey(X)) { ... map.get(Y) ... }
    where X and Y are different expressions.
    Returns a human-readable mismatch description, or None.

    Handles both Java and Python.
    """
    # Java: map.containsKey(X) ... map.get(Y)
    java_pat = re.compile(
        r"containsKey\s*\(\s*([^)]+?)\s*\)\s*\{.*?"
        r"get\s*\(\s*([^)]+?)\s*\)",
        re.DOTALL,
    )
    for m in java_pat.finditer(code):
        check_expr = _normalize_expr(m.group(1))
        get_expr = _normalize_expr(m.group(2))
        if check_expr and get_expr and check_expr != get_expr:
            return (f"containsKey({m.group(1).strip()}) checked but "
                    f"get({m.group(2).strip()}) retrieved — keys "
                    f"must match.")

    # Python: if X in dict: ... dict[Y]
    py_pat = re.compile(
        r"if\s+([a-zA-Z_]\w*)\s+in\s+([a-zA-Z_]\w*)\s*:\s*\n"
        r"(.*?)\n",
        re.DOTALL,
    )
    for m in py_pat.finditer(code):
        key_var = m.group(1).strip()
        dict_name = m.group(2).strip()
        body = m.group(3)
        # find dict[...] in the next few lines
        inner = re.search(
            rf"{re.escape(dict_name)}\[\s*([^\]]+?)\s*\]", body
        )
        if inner:
            retrieved = _normalize_expr(inner.group(1))
            checked = _normalize_expr(key_var)
            if checked and retrieved and checked != retrieved:
                return (f"Checked '{key_var}' but retrieved "
                        f"'{dict_name}[{inner.group(1).strip()}]'.")
    return None


def _normalize_expr(expr: str) -> str:
    """Loose normalization to compare expressions."""
    s = expr.strip()
    # Strip outer whitespace and any parentheses
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    # Remove spaces
    s = re.sub(r"\s+", "", s)
    return s


# ─────────────────────────────────────────────────────────────
# Parse claimed changes
# ─────────────────────────────────────────────────────────────
def _parse_claims(explanation: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "from_time": None, "to_time": None,
        "from_space": None, "to_space": None,
        "claims_replaced": [],
    }
    if not explanation:
        return out
    e = explanation.replace(" ", "")

    m = re.search(r"o\(([^)]+)\)(?:to|→|->)o\(([^)]+)\)", e)
    if m:
        out["from_time"] = f"O({m.group(1)})"
        out["to_time"] = f"O({m.group(2)})"

    m2 = re.search(
        r"o\(([^)]+)\)space(?:to|→|->)o\(([^)]+)\)space", e
    )
    if m2:
        out["from_space"] = f"O({m2.group(1)})"
        out["to_space"] = f"O({m2.group(2)})"

    repl_pat = re.compile(
        r"replaced\s+(\w+)\s+with\s+(\w+)"
        r"|switched\s+from\s+(\w+)\s+to\s+(\w+)"
    )
    for m in repl_pat.finditer(e):
        gs = [g for g in m.groups() if g]
        if len(gs) >= 2:
            out["claims_replaced"].append((gs[0], gs[1]))
    return out


def _contains_ds(code: str, name: str) -> bool:
    low = code.lower()
    aliases = {
        "hashmap": ["hashmap", "map<", "map.put", "map.get"],
        "hashset": ["hashset", "set<"],
        "array": ["int[", "int [", "arr[", "vector<"],
        "dict": ["dict", "{}"],
        "dictionary": ["dict"],
    }
    return any(a in low for a in aliases.get(name.lower(), [name.lower()]))


# ─────────────────────────────────────────────────────────────
# Pre-flight check: can this even be optimized?
# ─────────────────────────────────────────────────────────────
def optimization_not_needed(previous_code: str,
                            topic: Optional[str]) -> Tuple[bool, str]:
    """
    Return (True, reason) if the current solution is already
    asymptotically optimal for the given topic. HeadAgent uses this
    to skip code generation entirely.
    """
    if not previous_code:
        return (False, "")

    markers = _detect_markers(previous_code)
    time_c, space_c = _estimate_complexity(previous_code, markers)
    algo = _describe_algorithm(markers)

    t = (topic or "").lower()

    if "two sum" in t or "two-sum" in t:
        # HashMap is asymptotically optimal for unsorted two-sum
        if markers["hash_map"] and _rank(time_c) <= _rank("O(n)"):
            return (
                True,
                "The current HashMap-based Two Sum is already O(n) time "
                "and O(n) space — the proven optimal for unsorted input. "
                "No asymptotic improvement is available without changing "
                "the problem constraints (e.g., allowing input sorting, "
                "which would break index preservation).",
            )
        # Sort + two-pointer is also optimal time-wise
        if markers["sort"] and markers["two_pointer"]:
            return (
                True,
                "The current sort + two-pointer approach is O(n log n), "
                "which is the theoretical optimum for comparison-based "
                "sorting. No asymptotic improvement is available.",
            )

    if "palindrome" in t:
        if _rank(time_c) <= _rank("O(n)"):
            return (
                True,
                "A palindrome check requires reading every character at "
                "least once; O(n) is the theoretical minimum.",
            )

    if "binary search" in t:
        if _rank(time_c) <= _rank("O(log n)"):
            return (
                True,
                "Binary search is O(log n) — the theoretical minimum "
                "for comparison-based search in a sorted array.",
            )

    return (False, "")


# ─────────────────────────────────────────────────────────────
# Main validation
# ─────────────────────────────────────────────────────────────
def validate_optimization(
    original_code: str,
    new_code: str,
    original_expl: str = "",
    new_expl: str = "",
) -> Dict[str, Any]:
    if not original_code or not new_code:
        return {
            "verdict": "UNCERTAIN",
            "optimization_types": ["NONE"],
            "original": {}, "new": {},
            "claimed_improvement": False,
            "valid_improvement": False,
            "reason": "Missing original or new code.",
        }

    # ── Analyze both ────────────────────────────────────────
    orig_markers = _detect_markers(original_code)
    new_markers = _detect_markers(new_code)
    orig_time, orig_space = _estimate_complexity(
        original_code, orig_markers)
    new_time, new_space = _estimate_complexity(new_code, new_markers)
    orig_algo = _describe_algorithm(orig_markers)
    new_algo = _describe_algorithm(new_markers)

    # ── Key/retrieval mismatch (Semantic/Logic bug) ─────────
    mismatch = _detect_key_mismatch(new_code)
    if mismatch:
        return _fail(
            orig_time, orig_space, new_time, new_space,
            orig_algo, new_algo,
            f"Key/retrieval mismatch: {mismatch}",
            opt_types=["NONE"],
        )

    # ── Parse claims ────────────────────────────────────────
    claims = _parse_claims(
        new_expl or _extract_explanation(new_code)
    )

    # ── Cross-check claimed replacements ────────────────────
    for src, dst in claims["claims_replaced"]:
        if _contains_ds(new_code, src):
            return _fail(
                orig_time, orig_space, new_time, new_space,
                orig_algo, new_algo,
                f"Explanation claims '{src}' was replaced by '{dst}', "
                f"but new code still contains '{src}'.",
                opt_types=["NONE"],
            )
        if not _contains_ds(new_code, dst):
            return _fail(
                orig_time, orig_space, new_time, new_space,
                orig_algo, new_algo,
                f"Explanation claims '{dst}' was introduced, but new "
                f"code does not use it.",
                opt_types=["NONE"],
            )

    # ── Cross-check time/space claims ───────────────────────
    if claims["from_time"] and claims["to_time"]:
        if _rank(orig_time) != _rank(claims["from_time"]):
            return _fail(
                orig_time, orig_space, new_time, new_space,
                orig_algo, new_algo,
                f"Explanation claims original was {claims['from_time']} "
                f"but the original code is {orig_time}.",
            )
        if _rank(new_time) != _rank(claims["to_time"]):
            return _fail(
                orig_time, orig_space, new_time, new_space,
                orig_algo, new_algo,
                f"Explanation claims new is {claims['to_time']} but "
                f"the new code is {new_time}.",
            )

    # ── Determine improvement ───────────────────────────────
    time_improved = _is_improvement(orig_time, new_time)
    space_improved = _is_improvement(orig_space, new_space)

    opt_types: List[str] = []
    if time_improved:
        opt_types.append("TIME_OPTIMIZATION")
    if space_improved:
        opt_types.append("SPACE_OPTIMIZATION")

    valid = time_improved or space_improved

    # ── Short-circuit: NOT_NEEDED ───────────────────────────
    # If both complexities match the original AND the explanation
    # does NOT claim an improvement, treat as NOT_NEEDED.
    e = (new_expl or "").replace(" ", "").lower()
    claims_improvement = any(w in e for w in (
        "more efficient", "better performance", "faster",
        "reduce", "improve", "optimiz", "o(n²)→", "o(n^2)→",
        "→o(", "to o(",
    ))
    states_already_optimal = any(p in e for p in (
        "already optimal", "already asymptotically optimal",
        "no further optimization", "cannot be optimized further",
        "asymptotically optimal", "not needed",
    ))

    if not valid and not claims_improvement:
        return {
            "verdict": "NOT_NEEDED",
            "optimization_types": ["NONE"],
            "original": {"time": orig_time, "space": orig_space,
                          "algorithm": orig_algo},
            "new": {"time": new_time, "space": new_space,
                     "algorithm": new_algo},
            "claimed_improvement": False,
            "valid_improvement": False,
            "reason": (
                f"The current solution is already asymptotically "
                f"optimal at {orig_time} time and {orig_space} space. "
                f"No meaningful optimization is available."
            ),
        }

    if states_already_optimal:
        return {
            "verdict": "NOT_NEEDED",
            "optimization_types": ["NONE"],
            "original": {"time": orig_time, "space": orig_space,
                          "algorithm": orig_algo},
            "new": {"time": new_time, "space": new_space,
                     "algorithm": new_algo},
            "claimed_improvement": False,
            "valid_improvement": False,
            "reason": (
                "The current solution is already asymptotically optimal."
            ),
        }

    # ── FALSE claim detection ───────────────────────────────
    if not valid and claims_improvement:
        orig_rank = _rank(orig_time)
        new_rank = _rank(new_time)
        if new_rank > orig_rank and orig_rank >= 0 and new_rank >= 0:
            reason = (f"The proposed solution is SLOWER: "
                      f"{orig_time} → {new_time}. This is a "
                      f"regression, not an optimization.")
        elif new_rank == orig_rank:
            reason = (f"Same asymptotic complexity as original "
                      f"({orig_time}). No meaningful optimization.")
        else:
            reason = ("Explanation claims a performance improvement, "
                      "but no asymptotic advantage is demonstrated.")
        return _fail(
            orig_time, orig_space, new_time, new_space,
            orig_algo, new_algo, reason,
            opt_types=["NONE"],
        )

    # ── PASS (real improvement) ─────────────────────────────
    return {
        "verdict": "PASS",
        "optimization_types": opt_types or ["CONSTANT_FACTOR_OPTIMIZATION"],
        "original": {"time": orig_time, "space": orig_space,
                      "algorithm": orig_algo},
        "new": {"time": new_time, "space": new_space,
                 "algorithm": new_algo},
        "claimed_improvement": True,
        "valid_improvement": True,
        "reason": (f"{orig_algo} ({orig_time}) → "
                    f"{new_algo} ({new_time}). "
                    f"Improvement: {', '.join(opt_types) or 'none'}."),
    }


def _fail(orig_time: str, orig_space: str,
          new_time: str, new_space: str,
          orig_algo: str, new_algo: str,
          reason: str,
          opt_types: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "verdict": "FAIL",
        "optimization_types": opt_types or ["NONE"],
        "original": {"time": orig_time, "space": orig_space,
                      "algorithm": orig_algo},
        "new": {"time": new_time, "space": new_space,
                 "algorithm": new_algo},
        "claimed_improvement": True,
        "valid_improvement": False,
        "reason": reason,
    }