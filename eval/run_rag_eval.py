"""Retrieval evaluation for N3MemoryCore Free (needs the real embedding model).

Builds a throw-away DB from eval/rag_eval_set.json in a temporary N3MC_HOME (the real ~/.n3mc is
never touched), runs every query through hybrid_search and prints the metrics.

    python eval/run_rag_eval.py                         # current config.json defaults
    python eval/run_rag_eval.py --set vector_min_cos=0.8
    python eval/run_rag_eval.py --sweep vector_min_cos=0.80,0.82,0.85
    python eval/run_rag_eval.py --keep                  # keep the built DB, print its folder
    python eval/run_rag_eval.py --home <folder>         # reuse a DB built with --keep
    python eval/run_rag_eval.py --verbose               # list every miss / false alarm

Metrics (only rows in "## Top matches" count as strong):
  hit@1 / hit@3  the expected exchange is the 1st / within the first 3 strong rows
  MRR            mean of 1/rank over the strong rows (0 when absent)
  reach          the expected exchange is anywhere in the context (strong, weak or Q-A pairs)
  ver_ok         version queries where the newest exchange ranks above the old one
  fa             unanswerable queries that still get at least one strong row (false alarm)
  fa_unrel       unanswerable queries with a strong row that is not even related ("related" in the set)
  ctx            mean characters of the "# Recalled Memory Context" block the hook would inject
  noise/q        mean number of strong rows from other exchanges (answerable: not the expected
                 exchange or its old version; unanswerable: not related)
  strong/q       mean number of strong rows per query (context size)
"""
import argparse
import json
import os
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def _parse_value(v):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(v.lower(), v)


def build(m, data, conn, cfg):
    from n3memorycore.core.processor import add_chunk_prefixes, chunk_text

    turns = {}

    def save_turn(q, a):
        tid = str(uuid.uuid4())
        for role, text in (("user", q), ("claude", a)):
            for rec in add_chunk_prefixes(role, chunk_text(text, 400, 40)):
                m._store_record(conn, cfg, rec, "claude-code", None, "eval-store", tid)
        return tid

    for f in data["facts"]:
        turns[f["id"]] = save_turn(f["q"], f["a"])
    for d in data["distractors"]:
        save_turn("進捗は？" if any(ord(c) > 0x3000 for c in d) else "Any updates?", d)
    for f in data["facts"]:  # newer versions are saved last
        if "update" in f:
            turns[f["id"] + "@old"] = turns[f["id"]]
            turns[f["id"]] = save_turn(f["update"]["q"], f["update"]["a"])
    return turns


def evaluate(data, conn, cfg, turns, verbose=False):
    from n3memorycore.core.processor import hybrid_search, render_memory_context

    turn_name = {v: k for k, v in turns.items()}
    rows, lat = [], []
    for q in data["queries"]:
        t = time.perf_counter()
        res = hybrid_search(conn, q["q"], cfg, "eval-query")
        lat.append((time.perf_counter() - t) * 1000)
        strong = [r for r in res["results"] if not r.get("weak")]
        exp = turns[q["expect"]]
        rank = next((i + 1 for i, r in enumerate(strong) if r.get("turn_id") == exp), None)
        reach = exp in res["pairs"] or any(r.get("turn_id") == exp for r in res["results"])
        ver_ok = None
        if q["expect"] + "@old" in turns:
            old = turns[q["expect"] + "@old"]
            pos = {}
            for i, r in enumerate(res["results"]):
                pos.setdefault(r.get("turn_id"), i)
            ver_ok = exp in pos and (old not in pos or pos[exp] < pos[old])
        ok_turns = {exp, turns.get(q["expect"] + "@old")}
        noise = sum(r.get("turn_id") not in ok_turns for r in strong)
        ctx = len(render_memory_context(q["q"], res["results"], res["pairs"], res["pairs_omitted"]))
        rows.append({"q": q["q"], "kind": q["kind"], "rank": rank, "reach": reach, "ctx": ctx,
                     "ver_ok": ver_ok, "n_strong": len(strong), "noise": noise,
                     "top": (turn_name.get(strong[0].get("turn_id"), "distractor"),
                             strong[0]["content"][:50]) if strong else None})
    fa = []
    for q in data["unanswerable"]:
        res = hybrid_search(conn, q["q"], cfg, "eval-query")
        strong = [r for r in res["results"] if not r.get("weak")]
        rel = q.get("related", [])
        rel_turns = {turns[x] for x in rel if x in turns} | {turns.get(x + "@old") for x in rel}
        rel_text = [x for x in rel if x not in turns]

        def related(r):
            return r.get("turn_id") in rel_turns or any(t in r["content"] for t in rel_text)
        unrel = [r for r in strong if not related(r)]
        fa.append({"q": q["q"], "n_strong": len(strong), "noise": len(unrel),
                   "ctx": len(render_memory_context(q["q"], res["results"], res["pairs"], res["pairs_omitted"])),
                   "top": strong[0]["content"][:50] if strong else None,
                   "unrel_top": unrel[0]["content"][:50] if unrel else None})

    def group(sel):
        n = len(sel)
        return {"n": n,
                "hit1": sum(r["rank"] == 1 for r in sel),
                "hit3": sum(bool(r["rank"]) and r["rank"] <= 3 for r in sel),
                "mrr": round(sum(1 / r["rank"] for r in sel if r["rank"]) / n, 3) if n else 0,
                "reach": sum(r["reach"] for r in sel),
                "strong_per_q": round(sum(r["n_strong"] for r in sel) / n, 1) if n else 0,
                "noise_per_q": round(sum(r["noise"] for r in sel) / n, 1) if n else 0,
                "ctx": round(sum(r["ctx"] for r in sel) / n) if n else 0}

    kinds = {"ja-ja": lambda k: k.startswith("ja-ja"), "en-en": lambda k: k.startswith("en-en"),
             "cross": lambda k: k in ("ja-en", "en-ja"), "all": lambda k: True}
    out = {name: group([r for r in rows if f(r["kind"])]) for name, f in kinds.items()}
    vers = [r for r in rows if r["ver_ok"] is not None]
    out["version"] = {"n": len(vers), "ver_ok": sum(r["ver_ok"] for r in vers)}
    out["unanswerable"] = {"n": len(fa), "fa": sum(x["n_strong"] > 0 for x in fa),
                           "fa_unrel": sum(x["noise"] > 0 for x in fa),
                           "strong_per_q": round(sum(x["n_strong"] for x in fa) / len(fa), 1),
                           "noise_per_q": round(sum(x["noise"] for x in fa) / len(fa), 1),
                           "ctx": round(sum(x["ctx"] for x in fa) / len(fa))}
    out["latency_ms"] = {"mean": round(sum(lat) / len(lat)), "max": round(max(lat))}
    if verbose:
        for r in rows:
            if r["rank"] != 1:
                print(f"  MISS rank={r['rank']} reach={r['reach']} [{r['kind']}] {r['q']}  top={r['top']}")
        for x in fa:
            if x["n_strong"]:
                print(f"  FALSE ALARM strong={x['n_strong']} unrelated={x['noise']} {x['q']}  "
                      f"top={x['top']}  first_unrelated={x['unrel_top']}")
    return out


def print_summary(label, s):
    print(f"== {label}")
    for g in ("ja-ja", "en-en", "cross", "all"):
        v = s[g]
        print(f"  {g:6} n={v['n']:2} hit@1={v['hit1']:2} hit@3={v['hit3']:2} MRR={v['mrr']:.3f} "
              f"reach={v['reach']:2} strong/q={v['strong_per_q']} noise/q={v['noise_per_q']} ctx={v['ctx']}")
    print(f"  version ver_ok={s['version']['ver_ok']}/{s['version']['n']}  "
          f"unanswerable fa={s['unanswerable']['fa']}/{s['unanswerable']['n']} "
          f"fa_unrel={s['unanswerable']['fa_unrel']} noise/q={s['unanswerable']['noise_per_q']} "
          f"ctx={s['unanswerable']['ctx']}  "
          f"latency {s['latency_ms']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--sweep", metavar="KEY=V1,V2,...")
    ap.add_argument("--home", help="reuse a data folder built earlier with --keep")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", help="write the summaries to this file")
    a = ap.parse_args()

    home = a.home or tempfile.mkdtemp(prefix="n3mc-rag-eval-")
    os.environ["N3MC_HOME"] = home  # must be set before n3memorycore is imported
    from n3memorycore import n3memory as m

    with open(os.path.join(HERE, "rag_eval_set.json"), encoding="utf-8") as f:
        data = json.load(f)
    cfg = m.get_config()
    conn = m._open_db()
    turns_file = os.path.join(home, "eval_turns.json")
    if a.home and os.path.exists(turns_file):
        with open(turns_file, encoding="utf-8") as f:
            turns = json.load(f)
    else:
        t = time.perf_counter()
        turns = build(m, data, conn, cfg)
        with open(turns_file, "w", encoding="utf-8") as f:
            json.dump(turns, f)
        print(f"built {m.count_memories(conn)} records in {time.perf_counter() - t:.0f} s")

    base = dict(cfg)
    for kv in a.set:
        k, v = kv.split("=", 1)
        base[k] = _parse_value(v)
    variants = [("current" if not a.set else ", ".join(a.set), base)]
    if a.sweep:
        k, vs = a.sweep.split("=", 1)
        variants = [(f"{k}={v}", dict(base, **{k: _parse_value(v)})) for v in vs.split(",")]

    results = {}
    for label, c in variants:
        s = evaluate(data, conn, c, turns, a.verbose)
        print_summary(label, s)
        results[label] = s
    conn.close()
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    if a.keep or a.home:
        print("data folder:", home)


if __name__ == "__main__":
    main()
