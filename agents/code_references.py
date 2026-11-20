"""
Code references — small library of known-correct solutions.
===========================================================
Used by code_agent on RETRY when the reviewer rejects the output
for structural bugs. Small code models like qwen2.5-coder:1.5b
can't always translate review feedback into correct code, so we
give them a reference to adapt.

Only used on retry, never on first attempt. First-attempt failures
don't get a reference so the model learns from the retry loop.
"""
from __future__ import annotations
from typing import Dict, List, Optional


# Each entry: topic_key -> {"description": str, "python": str, "c": str, ...}
_REFERENCES: Dict[str, Dict[str, str]] = {
    "armstrong": {
        "description": "Check if a number is an Armstrong number "
                        "(sum of its digits raised to the number of "
                        "digits equals the number).",
        "python": '''def is_armstrong(n: int) -> bool:
    """Return True if n is an Armstrong number."""
    if n < 0:
        return False
    # Count digits
    digits = str(n)
    k = len(digits)
    # Sum each digit raised to the power k
    total = sum(int(d) ** k for d in digits)
    return total == n


if __name__ == "__main__":
    for v in (153, 407, 9474, 820):
        print(v, is_armstrong(v))''',

        "c": '''#include <stdio.h>
#include <math.h>

/*
 * Return 1 if `num` is an Armstrong number, else 0.
 * An Armstrong number of k digits equals the sum of its
 * digits each raised to the power k.
 */
int is_armstrong(long long num) {
    if (num < 0) return 0;

    /* Count the digits */
    int k = 0;
    long long temp = num;
    if (temp == 0) k = 1;
    while (temp != 0) {
        k++;
        temp /= 10;
    }

    /* Sum each digit raised to the power k */
    long long total = 0;
    temp = num;
    while (temp != 0) {
        int digit = (int)(temp % 10);
        long long power = 1;
        for (int i = 0; i < k; ++i) power *= digit;
        total += power;
        temp /= 10;
    }

    return total == num ? 1 : 0;
}

int main(void) {
    long long tests[] = {153, 407, 9474, 820};
    int n = (int)(sizeof(tests) / sizeof(tests[0]));
    for (int i = 0; i < n; ++i) {
        printf("%lld is %san Armstrong number\\n",
               tests[i], is_armstrong(tests[i]) ? "" : "not ");
    }
    return 0;
}''',

        "java": '''public class Armstrong {
    public static boolean isArmstrong(long num) {
        if (num < 0) return false;
        String digits = Long.toString(num);
        int k = digits.length();
        long total = 0;
        for (int i = 0; i < k; i++) {
            int d = digits.charAt(i) - '0';
            long power = 1;
            for (int j = 0; j < k; j++) power *= d;
            total += power;
        }
        return total == num;
    }

    public static void main(String[] args) {
        long[] tests = {153, 407, 9474, 820};
        for (long v : tests) {
            System.out.println(v + " is " +
                (isArmstrong(v) ? "" : "not ") +
                "an Armstrong number");
        }
    }
}''',

        "javascript": '''function isArmstrong(num) {
    if (num < 0) return false;
    const digits = String(num);
    const k = digits.length;
    let total = 0;
    for (const ch of digits) {
        total += Math.pow(parseInt(ch, 10), k);
    }
    return total === num;
}

for (const v of [153, 407, 9474, 820]) {
    console.log(`${v} is ${isArmstrong(v) ? "" : "not "}an Armstrong number`);
}''',
    },

    "two sum": {
        "description": "Return indices of two numbers that sum to target.",
        "python": '''def two_sum(nums, target):
    seen = {}
    for i, n in enumerate(nums):
        complement = target - n
        if complement in seen:
            return [seen[complement], i]
        seen[n] = i
    return None''',

        "c": '''#include <stdio.h>
#include <stdlib.h>

/* Returns 0 on success and writes indices to *i_out, *j_out.
 * Returns -1 if no pair exists. */
int two_sum(const int *nums, int n, int target,
            int *i_out, int *j_out) {
    for (int i = 0; i < n; ++i) {
        for (int j = i + 1; j < n; ++j) {
            if (nums[i] + nums[j] == target) {
                *i_out = i;
                *j_out = j;
                return 0;
            }
        }
    }
    return -1;
}

int main(void) {
    int nums[] = {2, 7, 11, 15};
    int target = 9;
    int i, j;
    if (two_sum(nums, 4, target, &i, &j) == 0) {
        printf("Indices: %d, %d\\n", i, j);
    } else {
        printf("No pair found\\n");
    }
    return 0;
}''',

        "java": '''import java.util.HashMap;
import java.util.Map;

public class TwoSum {
    public static int[] twoSum(int[] nums, int target) {
        Map<Integer, Integer> seen = new HashMap<>();
        for (int i = 0; i < nums.length; i++) {
            int complement = target - nums[i];
            if (seen.containsKey(complement)) {
                return new int[]{seen.get(complement), i};
            }
            seen.put(nums[i], i);
        }
        return null;
    }

    public static void main(String[] args) {
        int[] r = twoSum(new int[]{2, 7, 11, 15}, 9);
        System.out.println(java.util.Arrays.toString(r));
    }
}''',

        "javascript": '''function twoSum(nums, target) {
    const seen = {};
    for (let i = 0; i < nums.length; i++) {
        const complement = target - nums[i];
        if (complement in seen) return [seen[complement], i];
        seen[nums[i]] = i;
    }
    return null;
}

console.log(twoSum([2, 7, 11, 15], 9));''',
    },

    "palindrome": {
        "description": "Check if a string is a palindrome.",
        "python": '''def is_palindrome(s: str) -> bool:
    normalized = "".join(ch.lower() for ch in s if ch.isalnum())
    return normalized == normalized[::-1]''',

        "c": '''#include <stdio.h>
#include <string.h>
#include <ctype.h>

int is_palindrome(const char *s) {
    int left = 0;
    int right = (int)strlen(s) - 1;
    while (left < right) {
        while (left < right && !isalnum((unsigned char)s[left])) left++;
        while (left < right && !isalnum((unsigned char)s[right])) right--;
        if (tolower((unsigned char)s[left]) !=
            tolower((unsigned char)s[right])) return 0;
        left++;
        right--;
    }
    return 1;
}''',

        "java": '''public class Palindrome {
    public static boolean isPalindrome(String s) {
        int left = 0, right = s.length() - 1;
        while (left < right) {
            while (left < right && !Character.isLetterOrDigit(s.charAt(left))) left++;
            while (left < right && !Character.isLetterOrDigit(s.charAt(right))) right--;
            if (Character.toLowerCase(s.charAt(left)) !=
                Character.toLowerCase(s.charAt(right))) return false;
            left++;
            right--;
        }
        return true;
    }
}''',

        "javascript": '''function isPalindrome(s) {
    const clean = s.toLowerCase().replace(/[^a-z0-9]/g, "");
    return clean === clean.split("").reverse().join("");
}''',
    },

    "binary search": {
        "description": "Find target in a sorted array, return index or -1.",
        "python": '''def binary_search(arr, target):
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1''',

        "c": '''#include <stdio.h>

int binary_search(const int *arr, int n, int target) {
    int lo = 0, hi = n - 1;
    while (lo <= hi) {
        int mid = lo + (hi - lo) / 2;
        if (arr[mid] == target) return mid;
        if (arr[mid] < target) lo = mid + 1;
        else hi = mid - 1;
    }
    return -1;
}''',

        "java": '''public class BinarySearch {
    public static int search(int[] arr, int target) {
        int lo = 0, hi = arr.length - 1;
        while (lo <= hi) {
            int mid = lo + (hi - lo) / 2;
            if (arr[mid] == target) return mid;
            if (arr[mid] < target) lo = mid + 1;
            else hi = mid - 1;
        }
        return -1;
    }
}''',

        "javascript": '''function binarySearch(arr, target) {
    let lo = 0, hi = arr.length - 1;
    while (lo <= hi) {
        const mid = lo + ((hi - lo) >> 1);
        if (arr[mid] === target) return mid;
        if (arr[mid] < target) lo = mid + 1;
        else hi = mid - 1;
    }
    return -1;
}''',
    },
}


def find_reference(topic: Optional[str],
                   language: Optional[str]) -> Optional[str]:
    """
    Return a known-good reference solution for (topic, language),
    or None if we don't have one.

    Matches topic by substring so "code for armstrong numbers"
    hits the "armstrong" entry.
    """
    if not topic:
        return None
    t = topic.lower()

    for key, entry in _REFERENCES.items():
        if key in t:
            lang_key = _normalize_lang_key(language or "python")
            return entry.get(lang_key)
    return None


def _normalize_lang_key(language: str) -> str:
    l = language.lower()
    if "javascript" in l or l == "js":
        return "javascript"
    if "java" in l and "javascript" not in l:
        return "java"
    if "c++" in l or "cpp" in l:
        return "cpp"
    if "c#" in l or "csharp" in l:
        return "csharp"
    if l == "c":
        return "c"
    if "go" in l:
        return "go"
    if "rust" in l:
        return "rust"
    return "python"