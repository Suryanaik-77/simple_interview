"""
RAG accuracy test. Runs a fixed set of ground-truth questions (facts verified
against the lab HTML) plus out-of-scope questions against the live /api/ask
endpoint, and scores retrieval + answer correctness.

    python eval.py            # hits http://127.0.0.1:8100
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8100"

# Each case: question, keywords the answer MUST contain (case-insensitive, all
# required), and an optional lab substring expected among retrieved sources.
CASES = [
    ("How many RTL source files does the CVA6 RTL-to-Gate LEC testcase have?",
     ["174"], "CVA6"),
    ("Which command loads the SRAM macro libraries in the CVA6 LEC testcase?",
     ["read_db"], "CVA6"),
    ("What command loads the technology libraries in the AES Cipher Top RTL vs "
     "Netlist LEC with Clock Gating lab?",
     ["read_db", ".db"], "Clock Gating"),
    ("What set_top command is used for the AES cipher design in the clock "
     "gating LEC lab?",
     ["set_top"], "Clock Gating"),
    ("Which command generates the clock tree spec file in the CTS guided lab?",
     ["create_clock_tree_spec"], "Clock Tree"),
    ("What command performs design initialization for iguana_soc in Innovus?",
     ["init_design"], "Design Initialization"),
    ("What output file format is generated in the Chip Finish output generation "
     "lab?",
     ["def"], "Chip Finish"),
    ("What message indicates a successful run in the Formality LEC log?",
     ["verification succeeded"], None),
    ("Which command reads the RTL/Verilog source in the LEC flow?",
     ["read_verilog"], None),
    ("What make target runs the full flow?",
     ["make all"], None),
    ("What make target runs the verification checks?",
     ["make verify"], None),
    ("What kind of equivalence check is the Iguana SoC gate-to-gate testcase?",
     ["gate"], "Iguana"),
]

# Out-of-scope: the model should decline, not fabricate.
REFUSALS = [
    "What is the capital of France?",
    "How do I bake a chocolate cake?",
]

REFUSAL_MARKERS = ["don't have", "do not have", "not ", "no information",
                   "cannot", "isn't", "does not", "unable"]


def ask(question, k=5):
    # Score the direct-answer path: these are unambiguous ground-truth facts,
    # so disable the interactive clarify step (which would return no answer).
    body = json.dumps({"question": question, "k": k,
                       "allow_clarify": False}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/ask", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    ans_pass = ret_pass = 0
    print("=" * 78)
    for q, keywords, lab in CASES:
        d = ask(q)
        ans = d["answer"].lower()
        labs = " ".join(s["lab_name"] for s in d["sources"])
        ok_ans = all(kw.lower() in ans for kw in keywords)
        ok_ret = (lab is None) or (lab.lower() in labs.lower())
        ans_pass += ok_ans
        ret_pass += ok_ret
        print(f"[{'PASS' if ok_ans else 'FAIL'} ans | "
              f"{'PASS' if ok_ret else 'FAIL'} ret] {q[:60]}")
        if not ok_ans:
            print(f"     expected {keywords} | got: {d['answer'][:110]!r}")
        if not ok_ret:
            print(f"     expected lab ~{lab!r} | sources: {labs[:90]}")

    print("-" * 78)
    ref_pass = 0
    for q in REFUSALS:
        d = ask(q)
        ans = d["answer"].lower()
        declined = any(m in ans for m in REFUSAL_MARKERS)
        ref_pass += declined
        print(f"[{'PASS' if declined else 'FAIL'} refuse] {q}")
        if not declined:
            print(f"     got: {d['answer'][:110]!r}")

    print("=" * 78)
    n = len(CASES)
    print(f"Answer accuracy:    {ans_pass}/{n}  ({100*ans_pass//n}%)")
    print(f"Retrieval accuracy: {ret_pass}/{n}  ({100*ret_pass//n}%)")
    print(f"Refusal accuracy:   {ref_pass}/{len(REFUSALS)}")


if __name__ == "__main__":
    main()
