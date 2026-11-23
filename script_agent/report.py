from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .pipeline import write_json


CARD_SECTIONS = [
    ("一句话定位", "one_line_positioning"),
    ("第一印象", "first_impression"),
    ("深层性格与需求", "deeper_personality_and_needs"),
    ("最想得到", "most_wanted"),
    ("最害怕", "greatest_fears"),
    ("面对冲突", "conflict_pattern"),
    ("面对亲密关系", "intimacy_pattern"),
    ("核心矛盾", "central_contradictions"),
    ("语言与情绪表达", "speech_rhythm_wording_emotion"),
    ("通常不会说或做", "will_not_say_or_do"),
]


RELATIONSHIP_SECTIONS = [
    ("如何认识", "how_they_met"),
    ("表面关系", "surface_relation"),
    ("真实状态", "underlying_state"),
    ("A 如何理解 B", "a_view_of_b"),
    ("B 如何理解 A", "b_view_of_a"),
    ("A 想得到什么", "a_wants"),
    ("B 想得到什么", "b_wants"),
    ("主动权与控制权", "initiative_and_control"),
    ("共同经历", "important_shared_events"),
    ("当前冲突", "current_conflict"),
    ("未说出口的信息", "unspoken_information"),
    ("故事事实与用户视角", "story_truth_vs_audience_view"),
]


def render_all(bundle: Dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    graph = build_graph(bundle)
    write_json(output_dir / "relationship-graph.json", graph)
    (output_dir / "report.md").write_text(render_markdown(bundle), encoding="utf-8")
    (output_dir / "report.html").write_text(render_html(bundle, graph), encoding="utf-8")


def build_graph(bundle: Dict[str, Any]) -> Dict[str, Any]:
    nodes = [
        {"id": name, "label": name}
        for name in sorted(bundle.get("characters", {}).keys())
    ]
    edges = []
    for key, relationship in bundle.get("relationships", {}).items():
        edges.append(
            {
                "id": key,
                "source": relationship.get("person_a", ""),
                "target": relationship.get("person_b", ""),
                "label": relationship.get("current_stage", "待确认"),
                "timeline": relationship.get("timeline", []),
            }
        )
    return {"nodes": nodes, "edges": edges}


def render_markdown(bundle: Dict[str, Any]) -> str:
    meta = bundle.get("meta", {})
    lines = [
        "# 剧本角色与关系分析",
        "",
        "> {}".format(meta.get("review_warning", "所有内容须经人工确认。")),
        "",
        "剧本：{}  ".format(meta.get("source_name", "")),
        "生成时间：{}".format(meta.get("generated_at", "")),
        "",
        "## 角色卡",
        "",
    ]
    for name, card in bundle.get("characters", {}).items():
        lines.extend(_character_markdown(name, card))
    lines.extend(["## 人物关系档案", ""])
    for key, relationship in bundle.get("relationships", {}).items():
        lines.extend(_relationship_markdown(key, relationship))
    lines.extend(["## 信息差与防剧透台账", ""])
    ledger = bundle.get("knowledge_ledger", [])
    if not ledger:
        lines.extend(["暂无记录。", ""])
    for fact in ledger:
        lines.extend(
            [
                "- **{} / {}**：{}".format(
                    fact.get("scene_id", ""),
                    fact.get("audience_status", ""),
                    fact.get("fact", ""),
                ),
                "  - 已知：{}；未知：{}".format(
                    "、".join(fact.get("known_by", [])) or "未记录",
                    "、".join(fact.get("not_known_by", [])) or "未记录",
                ),
            ]
        )
    lines.extend(["", "## 编剧审核队列", ""])
    lines.append("共 {} 条需要确认；请优先使用 report.html 进行审阅。".format(len(bundle.get("review_queue", []))))
    lines.append("")
    return "\n".join(lines)


def _character_markdown(name: str, card: Dict[str, Any]) -> List[str]:
    lines = ["### {}".format(name), ""]
    aliases = card.get("aliases", [])
    if aliases:
        lines.extend(["别名：{}".format("、".join(aliases)), ""])
    for title, field in CARD_SECTIONS:
        lines.extend(_assertion_section(title, card.get(field, [])))
    arc = card.get("arc", {})
    lines.extend(
        [
            "#### 角色成长弧线",
            "",
            "- 开始：{}".format(arc.get("starting_state", "待确认")),
        ]
    )
    for stage in arc.get("stages", []):
        lines.append(
            "- {}：{}｜{}｜{}".format(
                stage.get("stage", "阶段"),
                stage.get("state", ""),
                stage.get("key_behavior", ""),
                stage.get("change_status", ""),
            )
        )
    lines.extend(
        [
            "- 结尾：{}".format(arc.get("ending_state", "待确认")),
            "- 总体变化：{}".format(arc.get("overall_change", "待确认")),
            "",
            "#### 待确认问题",
            "",
        ]
    )
    questions = card.get("unconfirmed_questions", [])
    lines.extend(["- {}".format(item) for item in questions] or ["- 暂无"])
    lines.append("")
    return lines


def _relationship_markdown(key: str, relationship: Dict[str, Any]) -> List[str]:
    lines = ["### {}".format(key), ""]
    for title, field in RELATIONSHIP_SECTIONS:
        lines.extend(_assertion_section(title, relationship.get(field, [])))
    lines.extend(["#### 关系变化线", ""])
    timeline = relationship.get("timeline", [])
    for item in timeline:
        lines.append(
            "- **{}**：{}｜变化事件：{}｜场景：{}".format(
                item.get("stage", "阶段"),
                item.get("state", ""),
                item.get("change_event", ""),
                "、".join(item.get("scene_ids", [])),
            )
        )
    if not timeline:
        lines.append("- 待确认")
    lines.extend(["", "当前阶段：{}".format(relationship.get("current_stage", "待确认")), ""])
    return lines


def _assertion_section(title: str, assertions: Iterable[Dict[str, Any]]) -> List[str]:
    lines = ["#### {}".format(title), ""]
    assertions = list(assertions)
    if not assertions:
        return lines + ["- 待确认", ""]
    for item in assertions:
        evidence = "、".join(item.get("evidence_ids", [])) or "无直接证据"
        lines.append(
            "- {} `[{} / {:.0%}]`（证据：{}）".format(
                item.get("claim", ""),
                item.get("review_status", "待确认"),
                float(item.get("confidence", 0)),
                evidence,
            )
        )
    lines.append("")
    return lines


def render_html(bundle: Dict[str, Any], graph: Dict[str, Any]) -> str:
    payload = json.dumps({"report": bundle, "graph": graph}, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(bundle.get("meta", {}).get("source_name", "剧本分析"))
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}｜角色与关系分析</title>
  <style>
    :root{{--ink:#18161f;--muted:#716b7d;--line:#e7e2ec;--panel:#fff;--bg:#f7f4fa;--purple:#6d3ad8;--purple-soft:#eee7ff;--warn:#a45a00}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
    header{{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;padding:18px 28px;background:rgba(247,244,250,.94);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}}
    h1{{font-size:19px;margin:0}} .warning{{color:var(--warn);font-size:13px}} nav{{display:flex;gap:8px;flex-wrap:wrap;padding:18px 28px 0;max-width:1400px;margin:auto}}
    button,.button{{border:1px solid var(--line);background:#fff;color:var(--ink);padding:9px 14px;border-radius:999px;cursor:pointer}} button.active{{background:var(--ink);color:#fff;border-color:var(--ink)}}
    main{{max-width:1400px;margin:auto;padding:22px 28px 60px}} section[hidden]{{display:none}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}}
    article,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 12px 35px rgba(37,23,58,.04)}} h2{{font-size:26px;margin:0 0 18px}} h3{{font-size:20px;margin:0 0 12px}} h4{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:22px 0 8px}}
    ul{{margin:0;padding-left:20px}} li{{margin:7px 0}} .tag{{display:inline-block;padding:2px 8px;margin-left:6px;border-radius:999px;background:var(--purple-soft);color:var(--purple);font-size:12px}} .muted{{color:var(--muted)}}
    .evidence{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--purple)}} .timeline{{border-left:2px solid var(--purple-soft);padding-left:18px;margin-left:6px}} .timeline-item{{position:relative;margin:14px 0}} .timeline-item:before{{content:"";position:absolute;width:9px;height:9px;border-radius:50%;background:var(--purple);left:-24px;top:8px}}
    #graph{{width:100%;min-height:560px;background:#fff;border:1px solid var(--line);border-radius:18px}} .graph-label{{font-size:12px;fill:var(--muted)}} .node-label{{font-size:14px;font-weight:700;fill:var(--ink)}}
    .review-item{{display:grid;grid-template-columns:130px 1fr 140px;gap:12px;align-items:start;padding:14px 0;border-bottom:1px solid var(--line)}} select,input{{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px;background:#fff}} .review-copy strong{{display:block}} .toolbar{{display:flex;gap:10px;margin-bottom:16px}} details{{margin-top:12px}} code{{white-space:pre-wrap}}
    @media(max-width:700px){{header{{align-items:flex-start;gap:8px;flex-direction:column;padding:15px 18px}}nav,main{{padding-left:18px;padding-right:18px}}.review-item{{grid-template-columns:1fr}}#graph{{min-height:420px}}}}
  </style>
</head>
<body>
  <header><h1>{title}｜角色与关系分析</h1><div class="warning">AI 输出不是正式设定，须由编剧确认</div></header>
  <nav id="tabs">
    <button data-tab="characters" class="active">角色卡</button>
    <button data-tab="relationships">关系档案</button>
    <button data-tab="graph">关系图</button>
    <button data-tab="knowledge">信息差</button>
    <button data-tab="review">编剧审核</button>
  </nav>
  <main>
    <section id="characters"></section>
    <section id="relationships" hidden></section>
    <section id="graph" hidden></section>
    <section id="knowledge" hidden></section>
    <section id="review" hidden></section>
  </main>
  <script>window.__REPORT__={payload};</script>
  <script>
  (()=>{{
    const data=window.__REPORT__.report, graph=window.__REPORT__.graph;
    const esc=s=>String(s??"").replace(/[&<>\"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}}[c]));
    const assertionList=items=>{{if(!items?.length)return '<p class="muted">待确认</p>';return '<ul>'+items.map(x=>`<li>${{esc(x.claim)}} <span class="tag">${{esc(x.review_status)}} · ${{Math.round((x.confidence||0)*100)}}%</span><div class="evidence">${{esc((x.evidence_ids||[]).join(' · ')||'无直接证据')}}</div></li>`).join('')+'</ul>'}};
    const sections={sections};
    const relationSections={relation_sections};
    document.querySelector('#characters').innerHTML='<h2>角色卡</h2><div class="grid">'+Object.entries(data.characters||{{}}).map(([name,c])=>`<article><h3>${{esc(name)}}</h3>${{c.aliases?.length?`<p class="muted">别名：${{esc(c.aliases.join('、'))}}</p>`:''}}${{sections.map(([title,key])=>`<h4>${{title}}</h4>${{assertionList(c[key])}}`).join('')}}<h4>成长弧线</h4><p><strong>开始：</strong>${{esc(c.arc?.starting_state||'待确认')}}</p><div class="timeline">${{(c.arc?.stages||[]).map(s=>`<div class="timeline-item"><strong>${{esc(s.stage)}}</strong><br>${{esc(s.state)}}<br><span class="muted">${{esc(s.key_behavior)}} · ${{esc(s.change_status)}}</span></div>`).join('')}}</div><p><strong>结尾：</strong>${{esc(c.arc?.ending_state||'待确认')}}</p><details><summary>海报视觉转译建议</summary><p>${{esc(c.poster_brief?.first_glance_message||'待角色卡确认后生成')}}</p><p class="muted">${{esc(c.poster_brief?.note||'')}}</p></details></article>`).join('')+'</div>';
    document.querySelector('#relationships').innerHTML='<h2>人物关系档案</h2><div class="grid">'+Object.entries(data.relationships||{{}}).map(([key,r])=>`<article><h3>${{esc(key)}}</h3>${{relationSections.map(([title,field])=>`<h4>${{title}}</h4>${{assertionList(r[field])}}`).join('')}}<h4>关系变化线</h4><div class="timeline">${{(r.timeline||[]).map(t=>`<div class="timeline-item"><strong>${{esc(t.stage)}}</strong><br>${{esc(t.state)}}<br><span class="muted">${{esc(t.change_event)}} · ${{esc((t.scene_ids||[]).join('、'))}}</span></div>`).join('')}}</div><p><strong>当前阶段：</strong>${{esc(r.current_stage||'待确认')}}</p></article>`).join('')+'</div>';
    document.querySelector('#knowledge').innerHTML='<h2>信息差与防剧透台账</h2><div class="grid">'+(data.knowledge_ledger||[]).map(f=>`<article><h3>${{esc(f.scene_id)}} · ${{esc(f.audience_status)}}</h3><p>${{esc(f.fact)}}</p><p><strong>已知：</strong>${{esc((f.known_by||[]).join('、')||'未记录')}}</p><p><strong>未知：</strong>${{esc((f.not_known_by||[]).join('、')||'未记录')}}</p><div class="evidence">${{esc((f.evidence_ids||[]).join(' · '))}}</div></article>`).join('')+'</div>';
    const reviewKey='script-agent-review-'+(data.meta?.source_digest||'draft'); const saved=JSON.parse(localStorage.getItem(reviewKey)||'{{}}');
    document.querySelector('#review').innerHTML=`<h2>编剧审核</h2><div class="toolbar"><button id="exportReview">导出审核结果</button><span class="muted">选择会自动保存在当前浏览器</span></div><div class="panel">${{(data.review_queue||[]).map((q,i)=>`<div class="review-item"><div><strong>${{esc(q.owner_type)}}</strong><br>${{esc(q.owner)}}</div><div class="review-copy"><strong>${{esc(q.claim)}}</strong><span class="evidence">${{esc((q.evidence_ids||[]).join(' · ')||'无直接证据')}}</span><input data-note="${{i}}" placeholder="编剧备注" value="${{esc(saved[i]?.notes||'')}}"></div><select data-decision="${{i}}">${{['待确认','人物理解','正式设定','驳回'].map(v=>`<option ${{(saved[i]?.decision||q.suggested_status)===v?'selected':''}}>${{v}}</option>`).join('')}}</select></div>`).join('')}}</div>`;
    const saveReview=()=>{{const state={{}};(data.review_queue||[]).forEach((q,i)=>{{state[i]={{decision:document.querySelector(`[data-decision="${{i}}"]`)?.value||'',notes:document.querySelector(`[data-note="${{i}}"]`)?.value||'',claim:q.claim,owner:q.owner,evidence_ids:q.evidence_ids}}}});localStorage.setItem(reviewKey,JSON.stringify(state));return state}};
    document.querySelector('#review').addEventListener('change',saveReview);document.querySelector('#review').addEventListener('input',saveReview);
    document.querySelector('#exportReview').addEventListener('click',()=>{{const blob=new Blob([JSON.stringify(saveReview(),null,2)],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='editor-review.json';a.click();URL.revokeObjectURL(a.href)}});
    function drawGraph(){{const host=document.querySelector('#graph');host.innerHTML='<h2 style="padding:22px 22px 0">动态人物关系图</h2>';const w=host.clientWidth||900,h=Math.max(480,Math.min(760,w*.65)),cx=w/2,cy=h/2+25,r=Math.max(100,Math.min(w,h)*.34),nodes=graph.nodes||[],positions={{}};nodes.forEach((n,i)=>{{const a=-Math.PI/2+(Math.PI*2*i/Math.max(1,nodes.length));positions[n.id]={{x:cx+Math.cos(a)*r,y:cy+Math.sin(a)*r}}}});const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${{w}} ${{h}}`);svg.setAttribute('width','100%');svg.setAttribute('height',h);(graph.edges||[]).forEach(e=>{{const a=positions[e.source],b=positions[e.target];if(!a||!b)return;const line=document.createElementNS(ns,'line');line.setAttribute('x1',a.x);line.setAttribute('y1',a.y);line.setAttribute('x2',b.x);line.setAttribute('y2',b.y);line.setAttribute('stroke','#bca8ec');line.setAttribute('stroke-width','3');svg.append(line);const label=document.createElementNS(ns,'text');label.setAttribute('x',(a.x+b.x)/2);label.setAttribute('y',(a.y+b.y)/2-7);label.setAttribute('text-anchor','middle');label.setAttribute('class','graph-label');label.textContent=e.label||'待确认';svg.append(label)}});nodes.forEach(n=>{{const p=positions[n.id],g=document.createElementNS(ns,'g'),circle=document.createElementNS(ns,'circle'),label=document.createElementNS(ns,'text');circle.setAttribute('cx',p.x);circle.setAttribute('cy',p.y);circle.setAttribute('r','34');circle.setAttribute('fill','#eee7ff');circle.setAttribute('stroke','#6d3ad8');circle.setAttribute('stroke-width','2');label.setAttribute('x',p.x);label.setAttribute('y',p.y+5);label.setAttribute('text-anchor','middle');label.setAttribute('class','node-label');label.textContent=n.label;g.append(circle,label);svg.append(g)}});if(!nodes.length){{const t=document.createElementNS(ns,'text');t.setAttribute('x',cx);t.setAttribute('y',cy);t.setAttribute('text-anchor','middle');t.setAttribute('class','graph-label');t.textContent='完成分析后将在这里显示关系图';svg.append(t)}}host.append(svg)}}
    document.querySelector('#tabs').addEventListener('click',e=>{{const b=e.target.closest('[data-tab]');if(!b)return;document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==b.dataset.tab);if(b.dataset.tab==='graph')drawGraph()}});
  }})();
  </script>
</body>
</html>""".format(
        title=title,
        payload=payload,
        sections=json.dumps(CARD_SECTIONS, ensure_ascii=False),
        relation_sections=json.dumps(RELATIONSHIP_SECTIONS, ensure_ascii=False),
    )

