// Pipeline run detail — renders Skrift agent audit events into a
// human-readable timeline, then live-tails new events over SSE.
//
// Each event is one row: an icon + a one-line summary + an expandable
// JSON details block. Raw payloads are kept verbatim under the toggle
// so the operator can drill in when the summary isn't enough.

(function () {
    const root = document.getElementById("pr-timeline");
    if (!root) return;
    const runId = root.getAttribute("data-run-id");
    const isRunning = root.getAttribute("data-is-running") === "true";

    // ── helpers ─────────────────────────────────────────────────

    function clip(s, max = 200) {
        if (typeof s !== "string") return s;
        s = s.replace(/\s+/g, " ").trim();
        return s.length > max ? s.slice(0, max) + "…" : s;
    }

    function fmtArgs(args) {
        if (args == null) return "()";
        if (typeof args === "string") return "(" + clip(args, 120) + ")";
        if (Array.isArray(args)) return "(" + args.map(v => fmtScalar(v)).join(", ") + ")";
        if (typeof args === "object") {
            const parts = Object.entries(args).map(([k, v]) => `${k}=${fmtScalar(v)}`);
            return "(" + parts.join(", ") + ")";
        }
        return "(" + String(args) + ")";
    }

    function fmtScalar(v) {
        if (v == null) return "null";
        if (typeof v === "string") return JSON.stringify(clip(v, 80));
        if (Array.isArray(v)) return `[${v.length} items]`;
        if (typeof v === "object") return `{${Object.keys(v).length} fields}`;
        return String(v);
    }

    function fmtTime(ts) {
        if (!ts) return "";
        try {
            const d = new Date(ts);
            return d.toLocaleTimeString();
        } catch (_) {
            return ts;
        }
    }

    // Map an event type to {icon, kind, summary, preview}. ``summary`` is
    // the headline ("→ web_search('postgres replication')"), ``preview``
    // is an optional second-line excerpt (e.g. the first 200 chars of a
    // long tool return).
    function renderEvent(ev) {
        const type = ev.type || "Event";
        const p = ev.payload || {};

        // Default fallback
        let icon = "•", kind = "", summary = type, preview = "";

        if (/ToolCallDispatched|ToolCallStarted/.test(type)) {
            icon = "→"; kind = "tool";
            const name = p.tool_name || p.name || "tool";
            summary = `${name}${fmtArgs(p.args)}`;
        } else if (/ToolCallExecuting/.test(type)) {
            icon = "⋯"; kind = "tool";
            const name = p.tool_name || p.name || "tool";
            summary = `${name} executing…`;
        } else if (/ToolCallCompleted/.test(type)) {
            icon = "←"; kind = "tool-ret";
            const name = p.tool_name || p.name || "tool";
            const result = p.result;
            let suffix = "";
            if (Array.isArray(result)) suffix = ` → [${result.length} items]`;
            else if (typeof result === "string") suffix = ` → ${JSON.stringify(clip(result, 80))}`;
            else if (typeof result === "object" && result) suffix = ` → {${Object.keys(result).length} fields}`;
            summary = `${name}${suffix}`;
            if (typeof result === "string") preview = clip(result, 240);
        } else if (/ToolCallErrored/.test(type)) {
            icon = "✗"; kind = "error";
            const name = p.tool_name || p.name || "tool";
            const err = (p.error && (p.error.message || p.error.exception_type)) || p.error || "errored";
            summary = `${name} errored — ${clip(String(err), 160)}`;
        } else if (/SubAgentDispatched/.test(type)) {
            icon = "↓"; kind = "subagent";
            const target = p.target_agent_name || p.agent_name || p.name || "sub-agent";
            summary = `dispatched ${target}`;
        } else if (/SubAgentCompleted/.test(type)) {
            icon = "↑"; kind = "subagent";
            const target = p.target_agent_name || p.agent_name || p.name || "sub-agent";
            summary = `${target} finished`;
        } else if (/UserMessageReceived/.test(type)) {
            icon = "✎"; kind = "message";
            const msg = p.message || p.content || "";
            const text = typeof msg === "string" ? msg : JSON.stringify(msg);
            summary = "user turn";
            preview = clip(text, 360);
        } else if (/AgentDefinition|AgentRegistered/.test(type)) {
            icon = "⚙"; kind = "";
            const name = p.name || p.agent_name || "agent";
            const model = p.model_id || p.model || (p.model_definition && p.model_definition.model_name) || "";
            summary = `agent ready — ${name}${model ? " (" + model + ")" : ""}`;
        } else if (/ModelResponse|AssistantMessage/.test(type)) {
            icon = "💬"; kind = "message";
            const text =
                p.text ||
                (p.message && (typeof p.message === "string" ? p.message : JSON.stringify(p.message))) ||
                (p.output && typeof p.output === "string" ? p.output : "") ||
                "";
            summary = "model response";
            preview = clip(text, 360);
        } else if (/RunCompleted|TurnCompleted/.test(type)) {
            icon = "✓"; kind = "subagent";
            summary = type.endsWith("RunCompleted") ? "run completed" : "turn completed";
        } else if (/RunFailed|Errored/.test(type)) {
            icon = "✗"; kind = "error";
            const err = (p.error && (p.error.message || p.error.exception_type)) || p.error || "failed";
            summary = clip(String(err), 200);
        } else if (/JobSubmitted/.test(type)) {
            icon = "⊕"; kind = "";
            summary = "job submitted";
        }

        return { icon, kind, summary, preview, type };
    }

    function stageHead(stageName, sessionId) {
        const head = document.createElement("div");
        head.className = "pr-stage-head";
        head.innerHTML = `
            <span>// <span class="stage-name"></span></span>
            <span style="opacity:.6;">session: <span class="sid"></span></span>
        `;
        head.querySelector(".stage-name").textContent = stageName;
        head.querySelector(".sid").textContent = (sessionId || "").slice(0, 8) + "…";
        return head;
    }

    function ensureStage(stageName, sessionId) {
        let stage = root.querySelector(
            `.pr-stage[data-session-id="${sessionId}"]`
        );
        if (stage) return stage;
        stage = document.createElement("div");
        stage.className = "pr-stage";
        stage.setAttribute("data-session-id", sessionId);
        stage.setAttribute("data-stage", stageName);
        stage.appendChild(stageHead(stageName, sessionId));
        const list = document.createElement("div");
        list.className = "pr-events";
        stage.appendChild(list);
        const empty = root.querySelector(".pr-empty");
        if (empty) empty.remove();
        root.appendChild(stage);
        return stage;
    }

    function renderRow(ev) {
        const meta = renderEvent(ev);
        const wrap = document.createElement("div");
        wrap.className = `pr-event kind-${meta.kind}`;
        if (ev.seq !== undefined) wrap.setAttribute("data-seq", String(ev.seq));
        wrap.innerHTML = `
            <div class="pr-event-row">
                <span class="pr-event-icon"></span>
                <div class="pr-event-body">
                    <div class="pr-event-title">
                        <span class="type"></span><span class="summary"></span>
                    </div>
                    <div class="pr-event-preview"></div>
                    <div class="pr-event-meta"></div>
                </div>
            </div>
            <div class="pr-event-details"><pre></pre></div>
        `;
        wrap.querySelector(".pr-event-icon").textContent = meta.icon;
        wrap.querySelector(".type").textContent = meta.type;
        wrap.querySelector(".summary").textContent = meta.summary;
        const previewEl = wrap.querySelector(".pr-event-preview");
        if (meta.preview) previewEl.textContent = meta.preview;
        else previewEl.remove();
        const metaParts = [];
        if (ev.ts) metaParts.push(fmtTime(ev.ts));
        if (ev.seq !== undefined) metaParts.push(`seq ${ev.seq}`);
        wrap.querySelector(".pr-event-meta").textContent = metaParts.join("  ·  ");

        const payload = ev.payload || {};
        wrap.querySelector("pre").textContent = JSON.stringify(payload, null, 2);

        wrap.querySelector(".pr-event-row").addEventListener("click", () => {
            wrap.classList.toggle("is-open");
        });
        return wrap;
    }

    function appendEvent(ev) {
        const stageName = ev.stage || "unknown";
        const sessionId = ev.session_id || stageName;
        const stage = ensureStage(stageName, sessionId);
        const list = stage.querySelector(".pr-events");
        if (ev.seq !== undefined) {
            const existing = list.querySelector(`.pr-event[data-seq="${ev.seq}"]`);
            if (existing) return;
        }
        list.appendChild(renderRow(ev));
    }

    // ── initial render from server-embedded JSON ───────────────

    function bootstrap() {
        const dataEl = document.getElementById("pr-initial-events");
        if (!dataEl) return;
        let bundle;
        try {
            bundle = JSON.parse(dataEl.textContent);
        } catch (_) {
            return;
        }
        const stages = bundle.stages || [];
        const events = bundle.events || [];
        // Pre-create stage cards in canonical order so they appear even
        // when a stage has zero recorded events yet.
        for (const pair of stages) {
            if (Array.isArray(pair) && pair.length === 2) {
                ensureStage(pair[0], pair[1]);
            }
        }
        for (const ev of events) {
            // Server's events carry session_id; align with ensureStage's
            // session-id-keyed cards.
            appendEvent(ev);
        }
    }

    bootstrap();

    // ── live SSE ───────────────────────────────────────────────

    if (!isRunning) return;
    const url = `/admin/blogging-agent/runs/${runId}/stream`;
    const source = new EventSource(url);

    source.addEventListener("audit", (e) => {
        let event;
        try { event = JSON.parse(e.data); } catch (_) { return; }
        appendEvent(event);
    });

    source.addEventListener("status", (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { return; }
        const pill = document.getElementById("pr-status-pill");
        if (pill && payload.status) {
            pill.className = `ba-pill ba-pill-${payload.status}`;
            pill.textContent = payload.status;
        }
        // Pre-create stage cards as their session_ids become known.
        const ids = payload.stage_session_ids || {};
        for (const [stage, sid] of Object.entries(ids)) {
            if (sid) ensureStage(stage, sid);
        }
        if (payload.status === "completed" || payload.status === "failed") {
            source.close();
            // Reload so the header buttons (View post / Publish for real)
            // refresh from the server.
            setTimeout(() => window.location.reload(), 800);
        }
    });

    source.addEventListener("error", () => {
        // EventSource will auto-retry. No-op here.
    });
})();
