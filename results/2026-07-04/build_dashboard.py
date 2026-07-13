#!/usr/bin/env python3
"""Regenerate EVERY data-bearing part of dashboard.html from summary_rich.json:
the ARMS block, the fact-vs-fiction observed column, and the conclusory subtitles.
Nothing in the dashboard is left hand-frozen, so a re-aggregation can't leave it stale."""
import json
import os
import re

D = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "summary_rich.json")))
A = D["arms"]
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
html = open(P).read()

STYLE = {"dasein": ('Parsec', "#10b981", "#047857"), "A0": ('Baseline', "#64748b", "#334155"),
         "woz": ('Woz', "#8b9bc4", "#5f6f9c"), "headroom": ('Headroom', "#c9a26b", "#a07f47"),
         "rtk": ('RTK', "#b48ab8", "#8c6690"),
         "caveman": ('Caveman', "#cc7a4d", "#9c552e")}

# ── 1. ARMS data block ──────────────────────────────────────────────────────
lines = ["const ARMS = {"]
for k in ("dasein", "A0", "woz", "headroom", "rtk", "caveman"):
    lab, c, e = STYLE[k]; v = A[k]
    lines.append(
        '  %s:%s{label:"%s", c:"%s", e:"%s", solved:%d, solve_rate:%d, cost_total:%.2f, '
        'cost_per_solved:%.2f, cost_list:%.2f, input_m:%.1f, output_m:%.2f, steps:%d, wall_h:%.2f, '
        'lat:%.2f, ctx_k:%.1f, hit:%.1f, crcw:%.1f, deaths:%d, vs_a0_cost_pct:%.1f, '
        'vs_a0_input_pct:%.1f, vs_a0_time_pct:%.1f},' % (
            k, " " * max(1, 9 - len(k)), lab, c, e, int(v["solved"]), int(v["solved"]),
            v["cost_total"], v["cost_per_solved"], v["cost_list_total"], v["input_tokens"] / 1e6,
            v["output_tokens"] / 1e6, int(v["steps_total"]), v["wall_h_total"], v["mean_latency_s"],
            v["max_prompt_mean"] / 1e3, v["cache_hit_rate"], v["cr_cw"], int(v["limit_deaths"]),
            v["vs_a0_cost_pct"], v["vs_a0_input_pct"], v["vs_a0_time_pct"]))
lines.append("};")
html = re.sub(r"const ARMS = \{.*?\n\};", "\n".join(lines), html, flags=re.S)

# ── 2. fact-vs-fiction observed column (data + a static mechanism note) ──────
def pct(x):
    return ("%+d%%" % round(x)).replace("+-", "-").replace("-", "−")
# claim text (advertised) + mechanism clause (static, qualitative) per layer
FVF = {
    "dasein": ('Curates the agent’s working context',
               ''),
    "woz": ('"Cut your Claude Code costs in half"; "30–40% faster" (an estimate)',
            'wall-clock slower, not faster'),
    "rtk": ('"60–90% fewer tokens on common dev commands"',
            'its hook rewrites only shell output — native Read/Grep/Glob bypass it'),
    "headroom": ('"60–95% fewer tokens, same answers"; "better caching"',
                 'its docs: "code passes through" uncompressed'),
    "caveman": ('"Cuts 65% of output tokens (measured)"; "full technical accuracy"',
                'the 65% is output-only — a small share of agentic-loop cost'),
}
rows = []
for k in ("dasein", "woz", "rtk", "headroom", "caveman"):
    v = A[k]; claim, note = FVF[k]
    obs = "%s cost, %s input, %d/100 solved" % (pct(v["vs_a0_cost_pct"]), pct(v["vs_a0_input_pct"]), int(v["solved"]))
    if note:
        obs += "; " + note
    rows.append('      <tr><td>%s</td><td class="claim">%s</td><td class="obs">%s</td></tr>'
                % (STYLE[k][0], claim, obs))
new_tbody = "<tbody>\n" + "\n".join(rows) + "\n    </tbody>"
html = re.sub(r"<tbody>\s*<tr><td>(?:Dasein|Parsec)</td>.*?</tbody>", new_tbody, html, flags=re.S)

# ── 3. de-conclusory subtitles (plain metric descriptions) ──────────────────
html = html.replace("Pick a KPI to compare across arms; the bars re-sort best to worst.",
                    "Pick a KPI to compare across arms; the bars re-sort highest to lowest.")
html = html.replace("against solve rate (right); the top-right corner is best. Hover any point.",
                    "against solve rate (right); the upper-right region is cheaper and solves more. Hover any point.")
html = html.replace("both versus the no-compression baseline; the top-right corner is best — saving on both. Hover any point.",
                    "both versus the no-compression baseline; the upper-right region saves on both. Hover any point.")

open(P, "w").write(html)
# report
print("dashboard regenerated. FVF observed column now:")
for k in ("dasein", "woz", "rtk", "headroom", "caveman"):
    v = A[k]
    print("  %-9s %s cost, %s input, %d/100" % (STYLE[k][0], pct(v["vs_a0_cost_pct"]), pct(v["vs_a0_input_pct"]), int(v["solved"])))
