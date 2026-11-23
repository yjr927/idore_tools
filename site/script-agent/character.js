(() => {
  "use strict";

  const state = {
    reportKey: "",
    run: null,
    report: null,
    characterEntries: [],
    selectedCharacterIndex: 0,
    relationshipEntries: [],
    map: null
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[char]));
  const firstClaim = (items, fallback = "待确认") => Array.isArray(items) && items.length ? items[0].claim || fallback : fallback;

  function scenePositionText(order, total = state.run?.scene_count || 1) {
    const index = Math.max(1, Number(order) || 1);
    if (total <= 1) return "全剧";
    if (index === 1) return "故事开场";
    if (index === total) return "故事结尾";
    const ratio = index / total;
    if (ratio <= .34) return "故事前段";
    if (ratio <= .67) return "故事中段";
    return "故事后段";
  }

  function sceneTextLabel(value) {
    const raw = String(value || "").trim();
    const match = raw.match(/^(S\d{4})/i);
    if (!match) return raw || "原文场景";
    const scene = (state.run?.scenes || []).find((item) => item.id === match[1]);
    if (!scene) return raw;
    const heading = String(scene.heading || "").trim();
    const generic = !heading || /^片段\s*\d+|^场景\s*\d+|^开场资料$/i.test(heading);
    const label = generic ? scenePositionText(scene.order) : heading;
    return /-E\d+/i.test(raw) ? `${label} · 原文依据` : label;
  }

  function evidenceText(ids, separator = "、") {
    return [...new Set(ids || [])].map(sceneTextLabel).join(separator);
  }

  function sectionMarker(label) {
    const markers = {
      "一句话人物形象": "概览", "快速版看到哪里": "范围", "继续深度分析会补充": "深入",
      "角色的一句话定位": "定位", "外在给人的第一印象": "印象", "更深层的性格和需求": "性格",
      "最想得到的东西": "欲望", "最害怕发生的事情": "恐惧", "面对冲突时的习惯反应": "冲突",
      "面对亲密关系时的行为模式": "关系", "角色最明显的性格矛盾": "矛盾",
      "说话的节奏、措辞和情绪表达方式": "表达", "角色通常不会说什么、不会做什么": "边界",
      "角色在故事开始和后期发生的变化": "变化", "支撑每一条判断的剧情或台词依据": "依据",
      "目前还无法确认的内容": "待定", "目前能确认的叙事信息": "已知",
      "为什么现在不能继续判断": "原因", "暂时不能确认的部分": "未知", "需要编剧补充或确认的问题": "待定"
    };
    return markers[label] || "内容";
  }

  function relationshipText(relation) {
    const stage = String(relation?.current_stage || "").trim();
    if (stage && stage !== "待确认") return stage;
    const surface = readableClaim(firstClaim(relation?.surface_relation, "关系待确认"));
    return surface.replace(/[。！？!?]/g, "").slice(0, 8) || "关系待确认";
  }

  function renderReport(report) {
    const characters = Object.entries(report.characters || {});
    const relationships = Object.entries(report.relationships || {});
    $("#characterBadge").textContent = characters.length;
    $("#relationshipBadge").textContent = relationships.length;
    $("#reviewBadge").textContent = (report.review_queue || []).length;
    $("#partialBanner").hidden = !report.meta?.partial_analysis;
    renderCharacters(characters);
    renderRelationships(relationships);
    renderKnowledge(report.knowledge_ledger || []);
    renderReview(report.review_queue || []);
  }

  function renderCharacters(characters) {
    const host = $("#charactersView");
    state.characterEntries = characters;
    if (!characters.length) { host.innerHTML = '<div class="empty-state">没有识别到可建立档案的角色。</div>'; return; }
    state.selectedCharacterIndex = Math.min(state.selectedCharacterIndex, characters.length - 1);
    drawCharacterFocus();
  }

  function drawCharacterFocus() {
    const host = $("#charactersView");
    const [name, card] = state.characterEntries[state.selectedCharacterIndex];
    const evidenceState = characterEvidenceState(card);
    const profileMode = card._meta?.profile_mode || (state.report?.meta?.analysis_depth === "preview" ? "preview" : "full");
    const isPreview = profileMode === "preview";
    const sections = evidenceState.limited
      ? limitedCharacterOutlineItems(card)
      : (isPreview ? quickCharacterOutlineItems(card) : characterOutlineItems(card));
    host.innerHTML = `
      <div class="character-dossier ${evidenceState.limited ? "evidence-limited" : ""}">
        <aside class="character-identity">
          <div class="identity-sticky">
            <p class="identity-kicker">CHARACTER / 人物档案</p>
            <div class="identity-portrait" aria-hidden="true">
              <div class="identity-head"></div><div class="identity-body"></div>
              <span>${escapeHtml(characterInitials(name))}</span>
            </div>
            <div class="identity-nameplate">
              <small>PRIMARY IDENTITY</small>
              <h3>${escapeHtml(name)}</h3>
              <em>${escapeHtml((card.aliases || []).join(" / ") || "暂无别名")}</em>
            </div>
            <p class="identity-positioning">“${escapeHtml(readableClaim(firstClaim(card.one_line_positioning), "角色的一句话定位"))}”</p>
            <div class="identity-status ${evidenceState.limited ? "limited" : ""}"><i></i><span>${evidenceState.limited ? "角色资料不足" : (isPreview ? "快速证据预览" : "AI 人物理解")}</span><small>${evidenceState.limited ? "暂不建立完整人设" : (isPreview ? "可按需深入分析" : "需编剧确认")}</small></div>
          </div>
        </aside>
        <section class="character-reading" aria-label="${escapeHtml(name)}完整角色卡">
          <header class="reading-head">
            <p>${evidenceState.limited ? "CHARACTER CARD AGENT / LIMITED EVIDENCE" : (isPreview ? "CHARACTER CARD AGENT / QUICK EVIDENCE PREVIEW" : "CHARACTER CARD AGENT / FULL PROFILE")}</p>
            <h3>${evidenceState.limited ? "这个角色目前能确认什么？" : (isPreview ? "用一句话先看懂这个角色" : "这个角色到底是什么样的人？")}</h3>
            <span>${evidenceState.limited ? `当前没有可定位的直接证据，因此不会为 ${escapeHtml(name)} 强行补全性格。` : (isPreview ? "快速概览。" : "判断可展开查看原文依据。")}</span>
          </header>
          ${evidenceState.limited ? `<div class="limited-notice"><strong>为什么没有完整角色卡？</strong><p>直接证据不足。</p></div>` : ""}
          <div class="character-outline-list">
            ${sections.map((section, index) => `
              <article class="outline-item ${section.tone || ""}">
                <div class="outline-index"><i></i><span>${escapeHtml(sectionMarker(section.label))}</span></div>
                <div class="outline-content">
                  <div class="outline-title"><h4>${escapeHtml(section.label)}</h4><small>${escapeHtml(section.english)}</small></div>
                  <div class="outline-claims">
                    ${section.rows.map((row) => `
                      <div class="outline-claim">
                        <div class="plain-claim"><span>简明结论</span><p>${escapeHtml(readableClaim(row.text, section.label))}</p></div>
                        <details class="claim-source">
                          <summary>查看完整判断${row.evidence?.length ? `与 ${row.evidence.length} 条依据` : ""}</summary>
                          <p>${escapeHtml(row.text)}</p>
                          ${row.note ? `<p class="claim-note"><strong>谨慎说明</strong>${escapeHtml(row.note)}</p>` : ""}
                          ${row.evidence?.length ? `<div class="claim-evidence"><span>原文依据</span>${row.evidence.map((id) => `<code title="${escapeHtml(id)}">${escapeHtml(sceneTextLabel(id))}</code>`).join("")}</div>` : '<div class="claim-evidence no-evidence"><span>尚无可定位依据</span></div>'}
                        </details>
                      </div>`).join("")}
                  </div>
                </div>
              </article>`).join("")}
          </div>
          <footer class="reading-foot"><span>AI GENERATED INTERPRETATION</span><p>没有充分证据的内容会标记为“待确认”，不自行补写。</p></footer>
        </section>
        <div class="character-switcher" aria-label="选择人物">
          <div class="switcher-label"><span>CHARACTER INDEX</span><small>点击姓名切换人物</small></div>
          <div class="character-name-list">
            ${state.characterEntries.map(([entryName], index) => `
              <button type="button" data-character-index="${index}" class="character-name-button ${index === state.selectedCharacterIndex ? "active" : ""}">
                <span class="mini-person">${escapeHtml(characterInitials(entryName))}</span>
                <strong>${escapeHtml(entryName)}</strong>
                <small>人物</small>
              </button>`).join("")}
          </div>
        </div>
      </div>`;
    $$("[data-character-index]", host).forEach((button) => button.addEventListener("click", () => {
      state.selectedCharacterIndex = Number(button.dataset.characterIndex);
      drawCharacterFocus();
      host.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  }

  function quickCharacterOutlineItems(card) {
    const item = (card.one_line_positioning || [])[0] || {};
    const evidence = [...new Set(item.evidence_ids || [])];
    return [
      {
        label: "一句话人物形象",
        english: "ONE-LINE CHARACTER OVERVIEW",
        rows: [{
          text: item.claim || "人物形象待确认",
          note: item.notes || "快速概览结论，深度分析后再补充依据。",
          evidence
        }]
      },
      {
        label: "快速版看到哪里",
        english: "QUICK COVERAGE",
        rows: [{
          text: evidence.length ? `主要参考场景：${evidenceText(evidence)}` : "目前没有足够场景支持稳定判断。",
          evidence: []
        }],
        tone: "evidence-section"
      },
      {
        label: "继续深度分析会补充",
        english: "AVAILABLE IN DEEP ANALYSIS",
        rows: [{
          text: "深层需求、欲望与恐惧、冲突模式、关系变化、成长弧线和逐条原文依据。",
          evidence: []
        }],
        tone: "uncertain-section"
      }
    ];
  }

  function characterOutlineItems(card) {
    const assertion = (label, english, field) => ({
      label,
      english,
      rows: Array.isArray(field) && field.length
        ? field.map((item) => ({ text: item.claim || "待确认", note: item.notes || "", evidence: [...new Set(item.evidence_ids || [])] }))
        : [{ text: "目前证据不足，等待编剧确认。", evidence: [] }]
    });
    const base = [
      assertion("角色的一句话定位", "ONE-LINE POSITIONING", card.one_line_positioning),
      assertion("外在给人的第一印象", "FIRST IMPRESSION", card.first_impression),
      assertion("更深层的性格和需求", "DEEPER PERSONALITY & NEEDS", card.deeper_personality_and_needs),
      assertion("最想得到的东西", "MOST WANTED", card.most_wanted),
      assertion("最害怕发生的事情", "GREATEST FEARS", card.greatest_fears),
      assertion("面对冲突时的习惯反应", "CONFLICT PATTERN", card.conflict_pattern),
      assertion("面对亲密关系时的行为模式", "INTIMACY PATTERN", card.intimacy_pattern),
      assertion("角色最明显的性格矛盾", "CENTRAL CONTRADICTIONS", card.central_contradictions),
      assertion("说话的节奏、措辞和情绪表达方式", "SPEECH & EMOTIONAL EXPRESSION", card.speech_rhythm_wording_emotion),
      assertion("角色通常不会说什么、不会做什么", "WOULD NOT SAY OR DO", card.will_not_say_or_do)
    ];
    const arc = card.arc || {};
    const arcRows = [
      { text: `故事开始：${arc.starting_state || "待确认"}`, evidence: [] },
      ...(arc.stages || []).map((stage) => ({ text: `${stage.stage || "变化阶段"}：${stage.state || "待确认"}${stage.key_behavior ? `；关键行为：${stage.key_behavior}` : ""}${stage.change_status ? `（${stage.change_status}）` : ""}`, evidence: [...new Set(stage.evidence_ids || [])] })),
      ...(arc.turning_points || []).map((point) => ({ text: `转折点${point.scene_id ? `（${sceneTextLabel(point.scene_id)}）` : ""}：${point.trigger || "触发事件待确认"}${point.before || point.after ? `；${point.before || "待确认"} → ${point.after || "待确认"}` : ""}`, evidence: [...new Set(point.evidence_ids || [])] })),
      { text: `故事后期：${arc.ending_state || "待确认"}`, evidence: [] },
      { text: `总体变化：${arc.overall_change || "待确认"}`, evidence: [] }
    ];
    const evidenceRows = base.flatMap((section) => {
      const ids = [...new Set(section.rows.flatMap((row) => row.evidence || []))];
      return ids.length ? [{ text: `${section.label}：${evidenceText(ids)}`, evidence: [] }] : [];
    });
    const arcEvidence = [...new Set(arcRows.flatMap((row) => row.evidence || []))];
    if (arcEvidence.length) evidenceRows.push({ text: `角色变化：${evidenceText(arcEvidence)}`, evidence: [] });
    return [
      ...base,
      { label: "角色在故事开始和后期发生的变化", english: "CHARACTER ARC", rows: arcRows },
      { label: "支撑每一条判断的剧情或台词依据", english: "EVIDENCE INDEX", rows: evidenceRows.length ? evidenceRows : [{ text: "尚未提取到可定位的原文证据。", evidence: [] }], tone: "evidence-section" },
      { label: "目前还无法确认的内容", english: "UNCONFIRMED QUESTIONS", rows: (card.unconfirmed_questions || []).length ? card.unconfirmed_questions.map((text) => ({ text, evidence: [] })) : [{ text: "暂无待确认问题。", evidence: [] }], tone: "uncertain-section" }
    ];
  }

  function characterEvidenceState(card) {
    const assertionFields = [
      "one_line_positioning", "first_impression", "deeper_personality_and_needs", "most_wanted",
      "greatest_fears", "conflict_pattern", "intimacy_pattern", "central_contradictions",
      "speech_rhythm_wording_emotion", "will_not_say_or_do"
    ];
    const ids = assertionFields.flatMap((field) => (card[field] || []).flatMap((item) => item.evidence_ids || []));
    ids.push(...(card.arc?.stages || []).flatMap((item) => item.evidence_ids || []));
    ids.push(...(card.arc?.turning_points || []).flatMap((item) => item.evidence_ids || []));
    const evidenceCount = new Set(ids).size;
    return { evidenceCount, limited: evidenceCount === 0 };
  }

  function limitedCharacterOutlineItems(card) {
    const knownRows = [...(card.one_line_positioning || []), ...(card.first_impression || [])]
      .map((item) => ({ text: item.claim || "待确认", note: item.notes || "", evidence: [...new Set(item.evidence_ids || [])] }));
    const unknownAreas = ["深层性格与需求", "核心欲望与恐惧", "冲突反应", "亲密关系模式", "性格矛盾", "语言风格", "行为底线", "角色变化"];
    return [
      {
        label: "目前能确认的叙事信息",
        english: "WHAT WE CURRENTLY KNOW",
        rows: knownRows.length ? knownRows : [{ text: "目前只能确认这个名字被剧本提及。", evidence: [] }]
      },
      {
        label: "为什么现在不能继续判断",
        english: "WHY THE PROFILE STOPS HERE",
        rows: [
          { text: "角色卡中没有可定位到原剧本的直接证据。", evidence: [] },
          { text: "他人的转述和间接线索，不能自动变成这个角色的性格、动机或关系事实。", evidence: [] }
        ],
        tone: "evidence-section"
      },
      {
        label: "暂时不能确认的部分",
        english: "NOT ENOUGH EVIDENCE YET",
        rows: [{ text: unknownAreas.join("、"), evidence: [] }]
      },
      {
        label: "需要编剧补充或确认的问题",
        english: "QUESTIONS FOR THE WRITER",
        rows: (card.unconfirmed_questions || []).length
          ? card.unconfirmed_questions.map((text) => ({ text, evidence: [] }))
          : [{ text: "需要补充这个角色的直接出场、台词、行动或可验证的原文。", evidence: [] }],
        tone: "uncertain-section"
      }
    ];
  }

  function readableClaim(value, sectionLabel = "") {
    const original = String(value || "待确认").replace(/\s+/g, " ").trim();
    const pendingPrefix = /^待确认[：:]\s*/.test(original);
    const uncertain = !pendingPrefix && /(尚不明确|尚无法确定|尚不能确认|无法确定|不足以确认|不足以建立|目前无法确定|现有材料不足以确认|待确认)/.test(original);
    if (uncertain) {
      const uncertaintyLabels = {
        "更深层的性格和需求": "深层性格与需求",
        "最想得到的东西": "最想得到什么",
        "最害怕发生的事情": "最害怕什么",
        "面对冲突时的习惯反应": "稳定的冲突模式",
        "面对亲密关系时的行为模式": "稳定的亲密关系模式",
        "角色最明显的性格矛盾": "核心性格矛盾",
        "说话的节奏、措辞和情绪表达方式": "说话与情绪表达方式",
        "角色通常不会说什么、不会做什么": "不会说或做什么"
      };
      return `${uncertaintyLabels[sectionLabel] || sectionLabel || "这项信息"}：暂时无法确认。`;
    }
    let text = original
      .replace(/^待确认[：:]\s*/, "")
      .replace(/（S\d{4}[^）]*）/g, "")
      .replace(/^(一个)?在当前场景中/, "")
      .replace(/^(一个)?在本场中/, "")
      .replace(/^一个在/, "")
      .replace(/^目前材料将 /, "")
      .replace(/ 呈现为/, "：")
      .replace(/、通过/g, "，只通过")
      .replace(/^首先呈现的是/, "")
      .replace(/^当下不易被外界接触持续打断：/, "抗拒外界接触：")
      .replace(/^本场选择可理解为：/, "")
      .replace(/^本场可推测/, "")
      .replace(/外界呼唤与抱持均未让这一行为持续停止/g, "外界干预仍无法阻止")
      .replace(/这一行为/g, "它")
      .replace(/的角色(?=[；。]|$)/g, "")
      .replace(/；其行为原因仍未揭示/g, "，原因未知")
      .replace(/；但.+$/g, "")
      .replace(/；不足以.+$/g, "")
      .replace(/；不能.+$/g, "")
      .trim();
    const sentence = text.split(/。/).find((part) => part.trim()) || text;
    const clauses = sentence.split(/[；;]/).map((part) => part.trim()).filter(Boolean);
    text = clauses[0] || sentence;
    if (text.length > 52) {
      const commaParts = text.split(/[，,]/).map((part) => part.trim()).filter(Boolean);
      let compact = "";
      for (const part of commaParts) {
        if (compact && `${compact}，${part}`.length > 45) break;
        compact = compact ? `${compact}，${part}` : part;
      }
      text = compact || `${text.slice(0, 45)}…`;
    }
    text = text.replace(/[；，\s]+$/g, "");
    return /[。！？!?]$/.test(text) ? text : `${text}。`;
  }

  function characterInitials(name) {
    const clean = String(name || "?").trim();
    return clean.slice(0, 2).toUpperCase();
  }

  function renderRelationships(relationships) {
    const host = $("#relationshipsView");
    state.relationshipEntries = relationships;
    host.innerHTML = `
      <div class="mindmap-shell">
        <section class="mindmap-board">
          <div class="mindmap-toolbar">
            <div><span>RELATIONSHIP FOCUS MAP</span><small>点击人物重排 · 点击关系名称查看详情</small></div>
            <div class="map-controls">
              <button type="button" data-map-action="out" aria-label="缩小">−</button>
              <button type="button" data-map-action="reset">居中</button>
              <button type="button" data-map-action="in" aria-label="放大">＋</button>
            </div>
          </div>
          <canvas id="relationshipCanvas" aria-label="可交互的人物关系思维导图"></canvas>
          <div class="map-legend"><span><i class="legend-focus"></i>当前中心人物</span><span><i class="legend-character"></i>直接相关人物</span><span><i class="legend-relation"></i>关系状态</span></div>
          <div class="map-character-index">${Object.keys(state.report?.characters || {}).map((name) => `<button type="button" data-map-character="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join("")}</div>
        </section>
        <aside class="map-detail" id="mapDetail">
          <p class="micro">HOW TO READ</p>
          <h3>人物关系思维导图</h3>
          <p>选择人物或关系查看详情。</p>
          <div class="map-empty-mark">＋</div>
        </aside>
      </div>`;
    setupMindMap();
  }

  function renderKnowledge(items) {
    $("#knowledgeView").innerHTML = items.length ? `<div class="knowledge-list">${items.map((item) => `
      <div class="knowledge-item"><code title="${escapeHtml(item.scene_id)}">${escapeHtml(sceneTextLabel(item.scene_id))}</code><p>${escapeHtml(item.fact)}</p><small>${escapeHtml(item.audience_status)}<br>已知：${escapeHtml((item.known_by || []).join("、") || "未记录")}<br>未知：${escapeHtml((item.not_known_by || []).join("、") || "未记录")}</small></div>`).join("")}</div>` : '<div class="empty-state">暂无信息差记录。</div>';
  }

  function renderReview(items) {
    const storageKey = `idore-review-${state.reportKey}`;
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch (_) { saved = {}; }
    const host = $("#reviewView");
    host.innerHTML = `<div class="review-toolbar"><span>共 ${items.length} 条判断需要人工确认</span><button class="text-button" id="exportReview" type="button">导出审核结果</button></div>
      <div>${items.map((item, index) => `
        <div class="review-row">
          <div class="review-owner">${escapeHtml(item.owner_type)}<br>${escapeHtml(item.owner)}</div>
          <div class="review-claim"><strong>${escapeHtml(item.claim)}</strong><small>${escapeHtml(evidenceText(item.evidence_ids || [], " · ") || "无直接证据")}</small></div>
          <div class="review-controls">
            <select data-review-decision="${index}" aria-label="审核结论">${["待确认", "人物理解", "正式设定", "驳回"].map((choice) => `<option ${((saved[index]?.decision || item.suggested_status) === choice) ? "selected" : ""}>${choice}</option>`).join("")}</select>
            <input data-review-note="${index}" aria-label="编剧备注" placeholder="编剧备注" value="${escapeHtml(saved[index]?.notes || "")}">
          </div>
        </div>`).join("")}</div>`;
    const save = () => {
      const output = {};
      items.forEach((item, index) => {
        output[index] = {
          owner: item.owner,
          claim: item.claim,
          evidence_ids: item.evidence_ids,
          decision: $(`[data-review-decision="${index}"]`, host)?.value || "",
          notes: $(`[data-review-note="${index}"]`, host)?.value || ""
        };
      });
      try { localStorage.setItem(storageKey, JSON.stringify(output)); } catch (_) { /* Export remains available in private browsing. */ }
      return output;
    };
    host.onchange = save;
    host.oninput = save;
    $("#exportReview", host)?.addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(save(), null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "editor-review.json";
      link.click();
      URL.revokeObjectURL(link.href);
    });
  }

  function bindTabs() {
    $("#resultTabs").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-tab]");
      if (!button) return;
      $$("button[data-tab]", $("#resultTabs")).forEach((item) => item.classList.toggle("active", item === button));
      const views = {
        characters: "#charactersView", relationships: "#relationshipsView",
        knowledge: "#knowledgeView", review: "#reviewView"
      };
      Object.entries(views).forEach(([name, selector]) => { $(selector).hidden = name !== button.dataset.tab; });
      if (button.dataset.tab === "relationships") window.setTimeout(() => resizeMindMap(true), 30);
    });
    window.addEventListener("resize", () => {
      if (state.map && !$("#relationshipsView").hidden) resizeMindMap(false);
    });
  }

  function setupMindMap() {
    const canvas = $("#relationshipCanvas");
    if (!canvas) return;
    const names = Object.keys(state.report?.characters || {});
    const degree = Object.fromEntries(names.map((name) => [name, 0]));
    state.relationshipEntries.forEach(([, relation]) => {
      if (relation.person_a in degree) degree[relation.person_a] += 1;
      if (relation.person_b in degree) degree[relation.person_b] += 1;
    });
    if (!names.length) { state.map = null; $("#relationshipsView").textContent = "暂无角色关系。"; return; }
    const initialFocus = [...names].sort((a, b) => degree[b] - degree[a] || a.localeCompare(b))[0] || "";
    state.map = {
      canvas,
      context: canvas.getContext("2d"),
      allNames: names,
      focusName: initialFocus,
      nodes: [],
      nodeLookup: {},
      edges: [],
      scale: 1,
      offsetX: 0,
      offsetY: 0,
      ratio: 1,
      width: 0,
      height: 0,
      dragging: false,
      moved: false,
      dragStartX: 0,
      dragStartY: 0,
      startOffsetX: 0,
      startOffsetY: 0,
      hitEdges: [],
      selection: null
    };
    focusMindMapCharacter(initialFocus, false);
    bindMindMapInteractions();
    $$("[data-map-character]", $("#relationshipsView")).forEach((button) => button.addEventListener("click", () => {
      focusMindMapCharacter(button.dataset.mapCharacter, true);
    }));
    $$("[data-map-action]", $("#relationshipsView")).forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.mapAction === "reset") resizeMindMap(true);
      else zoomMindMap(button.dataset.mapAction === "in" ? 1.18 : .84);
    }));
    resizeMindMap(true);
  }

  function focusMindMapCharacter(name, resetView = true) {
    const map = state.map;
    if (!map || !name) return;
    const related = state.relationshipEntries.filter(([, relation]) => relation.person_a === name || relation.person_b === name);
    const center = { id: name, type: "focus", label: name, x: 0, y: 0, width: 196, height: 76 };
    const nodes = [center];
    const edges = [];
    const count = Math.max(1, related.length);
    related.forEach(([key, relation], index) => {
      const otherName = relation.person_a === name ? relation.person_b : relation.person_a;
      const angle = -Math.PI / 2 + (Math.PI * 2 * index / count);
      const node = {
        id: otherName,
        type: "character",
        label: otherName,
        x: Math.cos(angle) * 360,
        y: Math.sin(angle) * 235,
        width: 176,
        height: 66
      };
      nodes.push(node);
      edges.push({
        id: key,
        index,
        label: relationshipText(relation),
        relation,
        start: center,
        end: node
      });
    });
    map.focusName = name;
    map.nodes = nodes;
    map.nodeLookup = Object.fromEntries(nodes.map((node) => [node.id, node]));
    map.edges = edges;
    map.selection = { type: "character", id: name };
    $$("[data-map-character]", $("#relationshipsView")).forEach((button) => button.classList.toggle("active", button.dataset.mapCharacter === name));
    showMapCharacter(name);
    if (resetView && map.width) fitMindMap();
    drawMindMap();
  }

  function resizeMindMap(resetPosition) {
    const map = state.map;
    if (!map) return;
    const box = map.canvas.getBoundingClientRect();
    if (box.width < 20 || box.height < 20) return;
    map.ratio = Math.min(2, window.devicePixelRatio || 1);
    map.width = box.width;
    map.height = box.height;
    map.canvas.width = Math.floor(box.width * map.ratio);
    map.canvas.height = Math.floor(box.height * map.ratio);
    if (resetPosition || !map.offsetX) fitMindMap();
    drawMindMap();
  }

  function fitMindMap() {
    const map = state.map;
    if (!map) return;
    const xs = map.nodes.map((node) => [node.x - node.width / 2, node.x + node.width / 2]).flat();
    const ys = map.nodes.map((node) => [node.y - node.height / 2, node.y + node.height / 2]).flat();
    const worldWidth = Math.max(...xs) - Math.min(...xs) + 160;
    const worldHeight = Math.max(...ys) - Math.min(...ys) + 160;
    map.scale = Math.max(.42, Math.min(1.1, map.width / worldWidth, map.height / worldHeight));
    map.offsetX = map.width / 2;
    map.offsetY = map.height / 2;
  }

  function drawMindMap() {
    const map = state.map;
    if (!map || !map.width) return;
    const context = map.context;
    context.setTransform(map.ratio, 0, 0, map.ratio, 0, 0);
    context.clearRect(0, 0, map.width, map.height);
    context.fillStyle = "#f8f5ff";
    context.fillRect(0, 0, map.width, map.height);
    context.strokeStyle = "#e4dcf4";
    context.lineWidth = 1;
    const grid = 44;
    const gridX = ((map.offsetX % (grid * map.scale)) + grid * map.scale) % (grid * map.scale);
    const gridY = ((map.offsetY % (grid * map.scale)) + grid * map.scale) % (grid * map.scale);
    for (let x = gridX; x < map.width; x += grid * map.scale) { context.beginPath(); context.moveTo(x, 0); context.lineTo(x, map.height); context.stroke(); }
    for (let y = gridY; y < map.height; y += grid * map.scale) { context.beginPath(); context.moveTo(0, y); context.lineTo(map.width, y); context.stroke(); }

    context.save();
    context.translate(map.offsetX, map.offsetY);
    context.scale(map.scale, map.scale);

    map.hitEdges = [];
    map.edges.forEach((edge) => {
      const start = edge.start, end = edge.end;
      const selected = map.selection?.type === "relationship" && map.selection.id === edge.id;
      context.strokeStyle = selected ? "#7549dc" : "#aa91da";
      context.lineWidth = (selected ? 4 : 2.5) / map.scale;
      context.beginPath();
      context.moveTo(start.x, start.y);
      context.lineTo(end.x, end.y);
      context.stroke();
      const midX = start.x + (end.x - start.x) * .58;
      const midY = start.y + (end.y - start.y) * .58;
      const label = edge.label;
      context.font = "700 12px 'Avenir Next', sans-serif";
      const labelWidth = Math.max(58, Math.min(136, context.measureText(label).width + 26));
      context.fillStyle = selected ? "#7045d6" : "#f1ebff";
      context.strokeStyle = selected ? "#4b2f67" : "#9a7bd2";
      context.lineWidth = 1.5 / map.scale;
      roundedRectPath(context, midX - labelWidth / 2, midY - 14, labelWidth, 28, 14);
      context.fill(); context.stroke();
      context.fillStyle = selected ? "#fff" : "#55336f";
      context.textAlign = "center"; context.textBaseline = "middle";
      context.fillText(trimCanvasText(context, label, labelWidth - 18), midX, midY + .5);
      map.hitEdges.push({ edge, x: midX - labelWidth / 2, y: midY - 14, width: labelWidth, height: 28 });
    });

    map.nodes.forEach((node) => drawMapNode(context, node, map));
    context.restore();
  }

  function drawMapNode(context, node, map) {
    const selected = map.selection?.type === "character" && map.selection.id === node.id;
    if (node.type === "focus") {
      context.fillStyle = "#7045d6";
      context.strokeStyle = "#4b2f67";
    } else {
      context.fillStyle = selected ? "#ffffff" : "#f2edff";
      context.strokeStyle = selected ? "#7045d6" : "#aa98c3";
    }
    context.lineWidth = (selected ? 4 : 2) / map.scale;
    roundedRectPath(context, node.x - node.width / 2, node.y - node.height / 2, node.width, node.height, 7);
    context.fill(); context.stroke();
    context.textAlign = "center"; context.textBaseline = "middle";
    if (node.type === "focus") {
      context.fillStyle = "#fff"; context.font = "800 12px sans-serif";
      context.fillText(trimCanvasText(context, node.label, 160), node.x, node.y - 7);
      context.fillStyle = "#eadfff"; context.font = "9px monospace";
      context.fillText("当前中心人物", node.x, node.y + 15);
    } else {
      context.fillStyle = "#151515"; context.font = "800 15px sans-serif";
      context.fillText(trimCanvasText(context, node.label, 145), node.x, node.y - 7);
      context.fillStyle = "#765895"; context.font = "9px monospace";
      context.fillText("CHARACTER NODE", node.x, node.y + 15);
    }
  }

  function roundedRectPath(context, x, y, width, height, radius) {
    const r = Math.min(radius, width / 2, height / 2);
    context.beginPath();
    context.moveTo(x + r, y);
    context.lineTo(x + width - r, y); context.quadraticCurveTo(x + width, y, x + width, y + r);
    context.lineTo(x + width, y + height - r); context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
    context.lineTo(x + r, y + height); context.quadraticCurveTo(x, y + height, x, y + height - r);
    context.lineTo(x, y + r); context.quadraticCurveTo(x, y, x + r, y);
    context.closePath();
  }

  function trimCanvasText(context, text, maxWidth) {
    const value = String(text || "");
    if (context.measureText(value).width <= maxWidth) return value;
    let output = value;
    while (output.length > 2 && context.measureText(`${output}…`).width > maxWidth) output = output.slice(0, -1);
    return `${output}…`;
  }

  function bindMindMapInteractions() {
    const map = state.map;
    const canvas = map.canvas;
    canvas.addEventListener("pointerdown", (event) => {
      map.dragging = true; map.moved = false;
      map.dragStartX = event.clientX; map.dragStartY = event.clientY;
      map.startOffsetX = map.offsetX; map.startOffsetY = map.offsetY;
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!map.dragging) return;
      const dx = event.clientX - map.dragStartX, dy = event.clientY - map.dragStartY;
      if (Math.abs(dx) + Math.abs(dy) > 4) map.moved = true;
      map.offsetX = map.startOffsetX + dx; map.offsetY = map.startOffsetY + dy;
      drawMindMap();
    });
    canvas.addEventListener("pointerup", (event) => {
      map.dragging = false; canvas.classList.remove("dragging");
      if (!map.moved) selectMindMapItem(event);
    });
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const box = canvas.getBoundingClientRect();
      zoomMindMap(event.deltaY < 0 ? 1.1 : .9, event.clientX - box.left, event.clientY - box.top);
    }, { passive: false });
  }

  function zoomMindMap(multiplier, anchorX = null, anchorY = null) {
    const map = state.map;
    if (!map) return;
    const x = anchorX ?? map.width / 2, y = anchorY ?? map.height / 2;
    const worldX = (x - map.offsetX) / map.scale, worldY = (y - map.offsetY) / map.scale;
    map.scale = Math.max(.35, Math.min(2.1, map.scale * multiplier));
    map.offsetX = x - worldX * map.scale; map.offsetY = y - worldY * map.scale;
    drawMindMap();
  }

  function selectMindMapItem(event) {
    const map = state.map;
    const box = map.canvas.getBoundingClientRect();
    const worldX = (event.clientX - box.left - map.offsetX) / map.scale;
    const worldY = (event.clientY - box.top - map.offsetY) / map.scale;
    const node = [...map.nodes].reverse().find((item) =>
      worldX >= item.x - item.width / 2 && worldX <= item.x + item.width / 2 &&
      worldY >= item.y - item.height / 2 && worldY <= item.y + item.height / 2
    );
    if (node) {
      focusMindMapCharacter(node.id, node.id !== map.focusName);
    } else {
      const hit = map.hitEdges.find((item) => worldX >= item.x && worldX <= item.x + item.width && worldY >= item.y && worldY <= item.y + item.height);
      if (hit) {
        map.selection = { type: "relationship", id: hit.edge.id };
        showMapRelationship(hit.edge.id, hit.edge.relation);
      }
    }
    drawMindMap();
  }

  function showMapCharacter(name) {
    const card = state.report?.characters?.[name] || {};
    $("#mapDetail").innerHTML = `
      <p class="micro">CHARACTER NODE</p><span class="map-detail-index">人物</span><h3>${escapeHtml(name)}</h3>
      <p class="map-detail-lede">${escapeHtml(firstClaim(card.one_line_positioning))}</p>
      <dl><dt>深层需求</dt><dd>${escapeHtml(firstClaim(card.deeper_personality_and_needs))}</dd><dt>核心矛盾</dt><dd>${escapeHtml(firstClaim(card.central_contradictions))}</dd><dt>总体变化</dt><dd>${escapeHtml(card.arc?.overall_change || "待确认")}</dd></dl>`;
  }

  function showMapRelationship(key, relation) {
    $("#mapDetail").innerHTML = `
      <p class="micro">RELATIONSHIP EDGE</p><span class="map-detail-index">${escapeHtml(relation.current_stage || "待确认")}</span><h3>${escapeHtml(key)}</h3>
      <p class="map-detail-lede">${escapeHtml(firstClaim(relation.underlying_state))}</p>
      <dl><dt>表面关系</dt><dd>${escapeHtml(firstClaim(relation.surface_relation))}</dd><dt>${escapeHtml(relation.person_a)} 如何理解对方</dt><dd>${escapeHtml(firstClaim(relation.a_view_of_b))}</dd><dt>${escapeHtml(relation.person_b)} 如何理解对方</dt><dd>${escapeHtml(firstClaim(relation.b_view_of_a))}</dd><dt>当前冲突</dt><dd>${escapeHtml(firstClaim(relation.current_conflict))}</dd></dl>
      <div class="map-timeline"><strong>关系变化线</strong>${(relation.timeline || []).map((item) => `<p><i></i><span>${escapeHtml(item.stage)}</span>${escapeHtml(item.state)}</p>`).join("") || '<p><i></i>暂无确认变化</p>'}</div>`;
  }


  // Match the local Script Character Agent's headings and bound oversized scenes.
  const heading = /^(?:第\s*[零〇一二两三四五六七八九十百千\d]+\s*[场幕集章]|场景\s*[零〇一二两三四五六七八九十百千\d]+|(?:INT\.?\/EXT\.?|I\/E\.?|INT\.?|EXT\.?)\s+|\d{1,4}\s*[.、]\s*(?:内景|外景|日|夜|晨|晚|INT|EXT)|(?:内景|外景)\s*[·.\-— ])/i;

  function splitScenes(text) {
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    const scenes = [];
    let label = "", current = [], size = 0;
    function flush() {
      const body = current.join("\n").trim();
      if (body) scenes.push({ title: label || "片段 " + (scenes.length + 1), body });
      current = []; size = 0;
    }
    for (const raw of lines) {
      const line = raw.trim();
      if (line && line.length <= 120 && heading.test(line)) {
        flush(); label = line;
      }
      if (size && size + raw.length > 14000) flush();
      current.push(raw);
      size += raw.length + 1;
    }
    flush();
    return scenes;
  }

  function drawPreview(scenes) {
    const list = $("#sceneList");
    list.replaceChildren();
    let index = 0;
    function next() {
      more.remove();
      const end = Math.min(index + 60, scenes.length);
      const fragment = document.createDocumentFragment();
      for (; index < end; index++) {
        const scene = scenes[index];
        const item = document.createElement("details");
        item.className = "scene-preview";
        const summary = document.createElement("summary");
        summary.textContent = scene.title;
        item.append(summary);
        item.addEventListener("toggle", () => {
          if (!item.open || item.childElementCount !== 1) return;
          const body = document.createElement("pre");
          body.textContent = scene.body;
          item.append(body);
        });
        fragment.append(item);
      }
      list.append(fragment);
      if (index < scenes.length) list.append(more);
    }
    const more = document.createElement("button");
    more.type = "button";
    more.className = "text-button";
    more.textContent = "显示更多场景";
    more.addEventListener("click", next);
    next();
  }

  const record = value => value && typeof value === "object" && !Array.isArray(value);
  let selection = 0;
  async function openDocument(file) {
    if (!file) return;
    const current = ++selection;
    const status = $("#fileStatus");
    try {
      const extension = file.name.split(".").pop().toLowerCase();
      if (!["json", "txt", "md", "fountain", "screenplay"].includes(extension)) {
        throw Error("请选择文本剧本或角色报告 JSON。PDF 和 DOCX 可在本机 Agent 中打开。");
      }
      if (file.size > 50 * 1024 * 1024) throw Error("文件超过 50 MB。");
      status.textContent = "正在读取…";
      const data = await file.arrayBuffer();
      if (current !== selection) return;
      const text = new TextDecoder().decode(data);
      let name = file.name, summary;
      if (extension === "json") {
        let report;
        try { report = JSON.parse(text); } catch (_) { throw Error("文件不是可读取的 JSON。"); }
        if (!record(report?.characters) || !record(report?.relationships) ||
            !Object.values(report.characters).every(record) || !Object.values(report.relationships).every(record) ||
            ![report.review_queue, report.knowledge_ledger].every(x => x == null || Array.isArray(x))) {
          throw Error("请选择 Script Character Agent 导出的 report.json。");
        }
        const digest = await crypto.subtle.digest("SHA-256", data);
        if (current !== selection) return;
        state.reportKey = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
        state.report = report;
        state.run = {scene_count:Number(report.meta?.scene_count_total) || 0};
        state.selectedCharacterIndex = 0;
        name = String(report.meta?.source_name || name);
        summary = Object.keys(report.characters).length + " 个角色";
        renderReport(report);
        $("#resultTabs button[data-tab='characters']").click();
        $("#sceneList").replaceChildren();
      } else {
        const scenes = splitScenes(text);
        if (!scenes.length) throw Error("剧本中没有可预览的文本。");
        summary = scenes.length + " 个场景";
        state.map = null;
        state.report = null;
        drawPreview(scenes);
      }
      $("#results").hidden = extension !== "json";
      $("#scenePanel").hidden = extension === "json";
      $("#projectName").textContent = name;
      $("#documentMeta").textContent = summary;
      $("#workspace").hidden = false;
      status.textContent = name;
      $("#workspace").scrollIntoView({behavior:"smooth", block:"start"});
    } catch (error) {
      if (current !== selection) return;
      $("#workspace").hidden = true;
      status.textContent = error instanceof TypeError ? "报告内容不完整，请重新导出。" : error.message;
    }
  }

  bindTabs();
  const input = $("#sourceFile"), zone = $("#dropzone");
  input.addEventListener("change", () => { const file = input.files[0]; input.value = ""; openDocument(file); });
  for (const type of ["dragenter", "dragover", "dragleave", "drop"]) {
    zone.addEventListener(type, event => {
      event.preventDefault();
      zone.classList.toggle("dragging", type === "dragenter" || type === "dragover");
      if (type === "drop") openDocument(event.dataTransfer.files[0]);
    });
  }
})();
