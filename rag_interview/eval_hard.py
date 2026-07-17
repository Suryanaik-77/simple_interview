"""
Harder RAG accuracy test: precision/negation, paraphrase, disambiguation, and a
hallucination trap (asking for a fact that does NOT exist in any lab -> the model
must decline rather than invent).

    python eval_hard.py
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8100"

# requirements: list of groups; each group is a set of acceptable substrings
# (ANY of the group must appear). ALL groups must be satisfied. lab = expected
# lab substring among sources (or None).
CASES = [
    ("In the CVA6 RTL-to-Gate LEC testcase, do I need to load standard-cell "
     ".db libraries, or only the SRAM macros?",
     [["only", "no ", "not "], ["sram"]], "CVA6"),
    ("How many report files must be generated in the CVA6 LEC testcase?",
     [["six", "6"]], "CVA6"),
    ("When must the SVF file be loaded relative to read_verilog in the LEC flow?",
     [["before"]], None),
    ("How do I confirm the synthesized netlist is functionally equivalent to "
     "the RTL?",
     [["verif", "equivalen", "match"]], None),
    ("Which command sets the design top module automatically in the clock "
     "gating LEC lab?",
     [["set_top"], ["auto"]], "Clock Gating"),
]

# Hallucination trap: SPEF parasitics are not present in these labs.
TRAP = ("What SPEF parasitic annotation file is loaded in the AES clock gating "
        "LEC lab?")
# Strict decline phrases (avoid loose 'not ' matching words like 'annotation').
REFUSAL_MARKERS = ["not available", "don't have", "do not have",
                   "does not contain", "not present", "not provided",
                   "not used", "not loaded", "no spef", "not mention",
                   "isn't in", "is not in the"]


def ask(question, k=5):
    body = json.dumps({"question": question, "k": k}).encode()
    req = urllib.request.Request(f"{BASE}/api/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    ans_pass = ret_pass = 0
    print("=" * 78)
    for q, groups, lab in CASES:
        d = ask(q)
        ans = d["answer"].lower()
        labs = " ".join(s["lab_name"] for s in d["sources"]).lower()
        ok_ans = all(any(alt in ans for alt in group) for group in groups)
        ok_ret = (lab is None) or (lab.lower() in labs)
        ans_pass += ok_ans
        ret_pass += ok_ret
        print(f"[{'PASS' if ok_ans else 'FAIL'} ans | "
              f"{'PASS' if ok_ret else 'FAIL'} ret] {q[:58]}")
        if not ok_ans:
            print(f"     need {groups} | got: {d['answer'][:130]!r}")

    print("-" * 78)
    d = ask(TRAP)
    ans = d["answer"].lower()
    # Pass if it declines AND does not assert a fabricated .spef filename.
    declined = any(m in ans for m in REFUSAL_MARKERS)
    fabricated = ".spef" in ans
    trap_pass = declined and not fabricated
    print(f"[{'PASS' if trap_pass else 'FAIL'} trap] {TRAP[:58]}")
    print(f"     -> {d['answer'][:160]!r}")

    print("=" * 78)
    n = len(CASES)
    print(f"Hard answer accuracy:    {ans_pass}/{n}")
    print(f"Hard retrieval accuracy: {ret_pass}/{n}")
    print(f"Hallucination trap:      {'PASS' if trap_pass else 'FAIL'}")


if __name__ == "__main__":
    main()
