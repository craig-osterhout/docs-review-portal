(function () {
  const ctx = window.REVIEW_CONTEXT;
  if (!ctx || !ctx.buildId || !ctx.pagePath) {
    return;
  }

  const REVIEWER_KEY = "docs-review-reviewer";
  const CONTENT_ROOT = document.querySelector("main") || document.body;
  const HAS_CUSTOM_HIGHLIGHT_API =
    typeof window.Highlight === "function" &&
    !!(window.CSS && window.CSS.highlights);
  const HIGHLIGHT_KEY_OPEN = "review-selection-open";
  const HIGHLIGHT_KEY_RESOLVED = "review-selection-resolved";
  let floatingButton = null;
  let pendingSelection = null;
  let showResolved = false;
  let activeCommentId = null;
  let commentsCache = [];
  let panel = null;
  let panelBody = null;
  let panelShowButton = null;
  let reviewerEntry = null;
  let reviewerInput = null;
  let reviewerHelp = null;
  let pendingReviewerAction = null;
  let reviewerBadge = null;

  assignLineNumbers();
  panel = createPanel();
  refreshComments();

  document.addEventListener("mouseup", onSelectionComplete);
  document.addEventListener("keyup", onSelectionComplete);
  document.addEventListener("click", onDocumentClick);
  window.addEventListener("scroll", onViewportChanged, true);
  window.addEventListener("resize", onViewportChanged);

  function assignLineNumbers() {
    let index = 1;
    const walker = document.createTreeWalker(CONTENT_ROOT, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const value = node.nodeValue || "";
      const parent = node.parentElement;
      if (
        value.trim() &&
        parent &&
        CONTENT_ROOT.contains(parent) &&
        !parent.closest("script,style,noscript,svg")
      ) {
        if (!parent.dataset.reviewLine) {
          parent.dataset.reviewLine = String(index);
          index += 1;
        }
      }
      node = walker.nextNode();
    }
  }

  function nearestLine(node) {
    let current = node;
    while (current) {
      if (
        current.nodeType === Node.ELEMENT_NODE &&
        current.dataset &&
        current.dataset.reviewLine
      ) {
        const value = parseInt(current.dataset.reviewLine, 10);
        if (!Number.isNaN(value)) {
          return value;
        }
      }
      current = current.parentNode;
    }
    return null;
  }

  function isNodeInsideRoot(node) {
    if (!node) {
      return false;
    }
    if (node === CONTENT_ROOT) {
      return true;
    }
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return !!(element && CONTENT_ROOT.contains(element));
  }

  function getNodePath(node) {
    const path = [];
    let current = node;
    while (current && current !== CONTENT_ROOT) {
      const parent = current.parentNode;
      if (!parent) {
        return null;
      }
      const index = Array.prototype.indexOf.call(parent.childNodes, current);
      if (index < 0) {
        return null;
      }
      path.unshift(index);
      current = parent;
    }
    return current === CONTENT_ROOT ? path : null;
  }

  function resolveNodePath(path) {
    if (!Array.isArray(path)) {
      return null;
    }
    let current = CONTENT_ROOT;
    for (const segment of path) {
      if (!current || !current.childNodes || segment < 0 || segment >= current.childNodes.length) {
        return null;
      }
      current = current.childNodes[segment];
    }
    return current || null;
  }

  function maxOffsetForNode(node) {
    if (!node) {
      return 0;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      return (node.nodeValue || "").length;
    }
    return node.childNodes ? node.childNodes.length : 0;
  }

  function serializeRange(range) {
    if (!range || !isNodeInsideRoot(range.startContainer) || !isNodeInsideRoot(range.endContainer)) {
      return null;
    }
    const startPath = getNodePath(range.startContainer);
    const endPath = getNodePath(range.endContainer);
    if (!startPath || !endPath) {
      return null;
    }
    return {
      startPath: startPath,
      startOffset: range.startOffset,
      endPath: endPath,
      endOffset: range.endOffset,
    };
  }

  function deserializeRange(selection) {
    if (!selection || typeof selection !== "object") {
      return null;
    }
    const startNode = resolveNodePath(selection.startPath);
    const endNode = resolveNodePath(selection.endPath);
    if (!startNode || !endNode) {
      return null;
    }
    const startOffset = Math.min(
      Number.isFinite(selection.startOffset) ? Number(selection.startOffset) : 0,
      maxOffsetForNode(startNode),
    );
    const endOffset = Math.min(
      Number.isFinite(selection.endOffset) ? Number(selection.endOffset) : 0,
      maxOffsetForNode(endNode),
    );
    try {
      const range = document.createRange();
      range.setStart(startNode, Math.max(0, startOffset));
      range.setEnd(endNode, Math.max(0, endOffset));
      if (range.collapsed) {
        return null;
      }
      return range;
    } catch (err) {
      return null;
    }
  }

  function onSelectionComplete() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      hideFloatingButton();
      return;
    }

    const text = sel.toString().trim();
    if (!text) {
      hideFloatingButton();
      return;
    }

    const range = sel.getRangeAt(0);
    const serializedSelection = serializeRange(range);
    if (!serializedSelection) {
      hideFloatingButton();
      return;
    }
    const startLine = nearestLine(range.startContainer);
    const endLine = nearestLine(range.endContainer);

    pendingSelection = {
      selectedText: text,
      selection: serializedSelection,
      lineStart:
        startLine != null && endLine != null ? Math.min(startLine, endLine) : null,
      lineEnd:
        startLine != null && endLine != null ? Math.max(startLine, endLine) : null,
      rect: range.getBoundingClientRect(),
    };
    showFloatingButton();
  }

  function showFloatingButton() {
    if (!pendingSelection) {
      return;
    }
    if (!floatingButton) {
      floatingButton = document.createElement("button");
      floatingButton.type = "button";
      floatingButton.textContent = "Add comment";
      floatingButton.className = "review-floating-action";
      floatingButton.addEventListener("click", addCommentFromSelection);
      document.body.appendChild(floatingButton);
    }

    const rect = pendingSelection.rect;
    floatingButton.style.display = "block";
    const margin = 10;
    const width = floatingButton.offsetWidth || 120;
    const height = floatingButton.offsetHeight || 30;
    let left = rect.left;
    let top = rect.bottom + 8;
    if (left + width > window.innerWidth - margin) {
      left = window.innerWidth - width - margin;
    }
    if (left < margin) {
      left = margin;
    }
    if (top + height > window.innerHeight - margin) {
      top = Math.max(margin, rect.top - height - 8);
    }
    floatingButton.style.left = `${Math.round(left)}px`;
    floatingButton.style.top = `${Math.round(top)}px`;
  }

  function hideFloatingButton() {
    if (floatingButton) {
      floatingButton.style.display = "none";
    }
    pendingSelection = null;
  }

  function getReviewerName() {
    return (window.sessionStorage.getItem(REVIEWER_KEY) || "").trim();
  }

  function setReviewerName(value) {
    const trimmed = (value || "").trim();
    if (!trimmed) {
      return "";
    }
    window.sessionStorage.setItem(REVIEWER_KEY, trimmed);
    if (reviewerInput && reviewerInput.value !== trimmed) {
      reviewerInput.value = trimmed;
    }
    updateReviewerBadge();
    clearReviewerPrompt();
    return trimmed;
  }

  function updateReviewerBadge() {
    if (!reviewerBadge) {
      return;
    }
    const reviewer = getReviewerName();
    if (reviewer) {
      reviewerBadge.textContent = `Reviewer: ${reviewer}`;
      reviewerBadge.classList.remove("is-missing");
    } else {
      reviewerBadge.textContent = "Reviewer: not set";
      reviewerBadge.classList.add("is-missing");
    }

    if (reviewerEntry) {
      reviewerEntry.hidden = !!reviewer;
    }
    reviewerBadge.hidden = !reviewer;
    if (!reviewer && reviewerInput) {
      reviewerInput.value = "";
    }
  }

  function clearReviewerPrompt() {
    if (reviewerHelp) {
      reviewerHelp.textContent = "";
    }
    if (reviewerInput) {
      reviewerInput.classList.remove("is-required");
    }
  }

  function requestReviewerName(afterSetAction) {
    setPanelVisible(true);
    if (typeof afterSetAction === "function") {
      pendingReviewerAction = afterSetAction;
    }
    if (reviewerHelp) {
      reviewerHelp.textContent = "Set your reviewer name to continue.";
    }
    if (reviewerInput) {
      reviewerInput.classList.add("is-required");
      reviewerInput.focus();
      reviewerInput.select();
    }
  }

  function beginReviewerEdit() {
    const current = getReviewerName();
    if (reviewerEntry) {
      reviewerEntry.hidden = false;
    }
    if (reviewerBadge) {
      reviewerBadge.hidden = true;
    }
    if (reviewerInput) {
      reviewerInput.value = current;
      reviewerInput.focus();
      reviewerInput.select();
    }
    clearReviewerPrompt();
  }

  function saveReviewerFromInput() {
    if (!reviewerInput) {
      return "";
    }
    const saved = setReviewerName(reviewerInput.value);
    if (!saved) {
      if (reviewerHelp) {
        reviewerHelp.textContent = "Reviewer name is required.";
      }
      reviewerInput.classList.add("is-required");
      reviewerInput.focus();
      return "";
    }
    const action = pendingReviewerAction;
    pendingReviewerAction = null;
    if (typeof action === "function") {
      window.setTimeout(function () {
        try {
          const maybePromise = action();
          if (maybePromise && typeof maybePromise.catch === "function") {
            maybePromise.catch((err) => console.error(err));
          }
        } catch (err) {
          console.error(err);
        }
      }, 0);
    }
    return saved;
  }

  function ensureReviewerName(promptIfMissing, onReadyAction) {
    const existing = getReviewerName();
    if (existing || !promptIfMissing) {
      return existing;
    }
    requestReviewerName(onReadyAction);
    return "";
  }

  function onDocumentClick(event) {
    const target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    if (target.closest(".review-comment")) {
      return;
    }
    clearActiveComment();
  }

  function setPanelVisible(visible) {
    if (!panel || !panelShowButton) {
      return;
    }
    if (visible) {
      panel.classList.remove("is-hidden");
    } else {
      panel.classList.add("is-hidden");
    }
    panelShowButton.classList.toggle("is-active", visible);
    panelShowButton.setAttribute("aria-expanded", visible ? "true" : "false");
    const label = visible ? "Hide comments panel" : "Show comments panel";
    panelShowButton.title = label;
    panelShowButton.setAttribute("aria-label", label);
  }

  function createPanel() {
    const aside = document.createElement("aside");
    aside.className = "review-panel";
    aside.innerHTML = `
      <button type="button" class="review-panel-show is-active" id="review-panel-toggle" aria-expanded="true" title="Hide comments panel" aria-label="Hide comments panel">
        <span class="review-panel-show-text">Comments</span>
      </button>
      <div class="review-panel-content">
        <div class="review-panel-head">
          <h2>Comments</h2>
          <div class="review-panel-head-actions">
            <a class="review-icon-button" id="review-view-all-comments" href="/comments?build_id=${encodeURIComponent(String(ctx.buildId))}" target="_blank" rel="noreferrer" title="View all comments" aria-label="View all comments">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M21 12a8.5 8.5 0 0 1-8.5 8.5H6l-3 3v-6.5A8.5 8.5 0 1 1 21 12z"></path>
              </svg>
            </a>
            <button type="button" class="review-icon-button" id="review-change-reviewer" title="Change reviewer name" aria-label="Change reviewer name">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"></path>
                <path d="M5 20a7 7 0 0 1 14 0"></path>
              </svg>
            </button>
            <button type="button" class="review-icon-button" id="review-toggle-resolved" title="Show resolved comments" aria-label="Show resolved comments">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M4 7h10"></path>
                <path d="M4 12h10"></path>
                <path d="M4 17h10"></path>
                <path d="M17 13l2 2 3-3"></path>
              </svg>
            </button>
          </div>
        </div>
        <div class="review-reviewer-row">
          <div class="review-reviewer-entry" id="review-reviewer-entry">
            <label for="review-reviewer-input">Reviewer name</label>
            <div class="review-reviewer-controls">
              <input type="text" id="review-reviewer-input" autocomplete="name" maxlength="120" placeholder="Enter your name" />
              <button type="button" id="review-reviewer-save">Set name</button>
            </div>
            <div class="review-reviewer-help" id="review-reviewer-help" aria-live="polite"></div>
          </div>
          <span class="review-current-reviewer" id="review-current-reviewer">Reviewer: not set</span>
        </div>
        <div class="review-comment-hint">Highlight text on the page to add comments.</div>
        <div id="review-panel-body">Loading...</div>
      </div>
    `;
    document.body.appendChild(aside);

    panelShowButton = aside.querySelector("#review-panel-toggle");
    if (panelShowButton) {
      panelShowButton.addEventListener("click", function () {
        const currentlyHidden = panel && panel.classList.contains("is-hidden");
        setPanelVisible(!!currentlyHidden);
      });
    }

    panelBody = aside.querySelector("#review-panel-body");
    reviewerEntry = aside.querySelector("#review-reviewer-entry");
    reviewerBadge = aside.querySelector("#review-current-reviewer");
    reviewerInput = aside.querySelector("#review-reviewer-input");
    reviewerHelp = aside.querySelector("#review-reviewer-help");
    const reviewerSaveButton = aside.querySelector("#review-reviewer-save");
    if (reviewerInput) {
      reviewerInput.value = getReviewerName();
      reviewerInput.addEventListener("input", clearReviewerPrompt);
      reviewerInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          saveReviewerFromInput();
        }
      });
    }
    if (reviewerSaveButton) {
      reviewerSaveButton.addEventListener("click", saveReviewerFromInput);
    }
    updateReviewerBadge();

    const changeReviewerButton = aside.querySelector("#review-change-reviewer");
    if (changeReviewerButton) {
      changeReviewerButton.addEventListener("click", beginReviewerEdit);
    }

    const toggleButton = aside.querySelector("#review-toggle-resolved");
    updateResolvedToggleLabel(toggleButton);
    if (toggleButton) {
      toggleButton.addEventListener("click", function () {
        showResolved = !showResolved;
        updateResolvedToggleLabel(toggleButton);
        renderComments(commentsCache);
      });
    }

    setPanelVisible(true);
    return aside;
  }

  function onViewportChanged() {
    if (!pendingSelection) {
      return;
    }
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      hideFloatingButton();
      return;
    }
    pendingSelection.rect = selection.getRangeAt(0).getBoundingClientRect();
    showFloatingButton();
  }

  function updateResolvedToggleLabel(toggleButton) {
    if (!toggleButton) {
      return;
    }
    const label = showResolved
      ? "Hide resolved comments"
      : "Show resolved comments";
    toggleButton.title = label;
    toggleButton.setAttribute("aria-label", label);
    toggleButton.classList.toggle("is-active", showResolved);
  }

  function clonePendingSelection(selectionData) {
    if (!selectionData) {
      return null;
    }
    return {
      lineStart: selectionData.lineStart,
      lineEnd: selectionData.lineEnd,
      selectedText: selectionData.selectedText,
      selection: selectionData.selection
        ? JSON.parse(JSON.stringify(selectionData.selection))
        : null,
    };
  }

  async function addCommentForSelection(selectionData) {
    if (!selectionData) {
      return;
    }
    const reviewer = ensureReviewerName(true, function () {
      return addCommentForSelection(selectionData);
    });
    if (!reviewer) {
      return;
    }
    const body = window.prompt("Add comment");
    if (!body || !body.trim()) {
      return;
    }
    const payload = {
      build_id: ctx.buildId,
      page_path: ctx.pagePath,
      line_start: selectionData.lineStart,
      line_end: selectionData.lineEnd,
      selected_text: selectionData.selectedText,
      selection: selectionData.selection,
      body: body.trim(),
      reviewer: reviewer,
    };
    try {
      await api("/api/comments", payload);
      const selection = window.getSelection();
      if (selection) {
        selection.removeAllRanges();
      }
      hideFloatingButton();
      await refreshComments();
    } catch (err) {
      console.error(err);
      window.alert("Could not create comment.");
    }
  }

  async function addCommentFromSelection() {
    if (!pendingSelection) {
      return;
    }
    const selectionData = clonePendingSelection(pendingSelection);
    await addCommentForSelection(selectionData);
  }

  async function refreshComments() {
    const params = new URLSearchParams({
      build_id: String(ctx.buildId),
      page_path: String(ctx.pagePath),
    });
    try {
      const response = await fetch(`/api/comments?${params.toString()}`);
      const data = await response.json();
      commentsCache = Array.isArray(data.comments) ? data.comments : [];
      renderComments(commentsCache);
    } catch (err) {
      console.error(err);
      renderError("Failed to load comments.");
    }
  }

  function renderError(message) {
    if (panelBody) {
      panelBody.textContent = message;
    }
  }

  function renderComments(comments) {
    if (!panelBody) {
      return;
    }
    const filteredComments = showResolved
      ? comments
      : comments.filter((comment) => !comment.resolved);

    panelBody.innerHTML = "";
    if (!filteredComments.length) {
      panelBody.innerHTML =
        "<div class='review-comment-meta'>No visible comments on this page.</div>";
      clearActiveComment();
      return;
    }

    filteredComments.forEach((comment) => {
      panelBody.appendChild(renderComment(comment));
    });

    if (jumpToCommentIfRequested(filteredComments)) {
      return;
    }

    const active = filteredComments.find(
      (comment) => Number(comment.id) === Number(activeCommentId),
    );
    if (active) {
      setActiveComment(active, false);
    } else {
      clearActiveComment();
    }
  }

  function renderComment(comment) {
    const card = document.createElement("article");
    card.className = `review-comment${comment.resolved ? " resolved" : ""}`;
    card.id = `review-comment-${comment.id}`;

    const header = document.createElement("div");
    header.className = "review-comment-head";
    header.innerHTML = `
      <strong>${escapeHtml(String(comment.reviewer || "reviewer"))}</strong>
      <span>${comment.resolved ? "resolved" : "open"}</span>
    `;
    card.appendChild(header);

    const meta = document.createElement("div");
    meta.className = "review-comment-meta";
    meta.textContent = `${comment.created_at_human || ""}`;
    card.appendChild(meta);

    const text = document.createElement("p");
    text.textContent = comment.body || "";
    card.appendChild(text);

    const actions = document.createElement("div");
    actions.className = "review-actions";

    const resolveButton = document.createElement("button");
    resolveButton.type = "button";
    resolveButton.textContent = comment.resolved ? "Mark open" : "Mark resolved";
    resolveButton.addEventListener("click", async function (event) {
      event.stopPropagation();
      await toggleResolved(comment.id, !comment.resolved);
    });
    actions.appendChild(resolveButton);

    const replyButton = document.createElement("button");
    replyButton.type = "button";
    replyButton.textContent = "Reply";
    replyButton.addEventListener("click", async function (event) {
      event.stopPropagation();
      await replyTo(comment.id);
    });
    actions.appendChild(replyButton);

    card.appendChild(actions);

    if (Array.isArray(comment.replies)) {
      comment.replies.forEach((reply) => {
        const replyEl = document.createElement("div");
        replyEl.className = "review-reply";
        replyEl.innerHTML = `
          <strong>${escapeHtml(String(reply.reviewer || "reviewer"))}</strong>
          <div class="review-comment-meta">${escapeHtml(String(reply.created_at_human || ""))}</div>
          <div>${escapeHtml(String(reply.body || ""))}</div>
        `;
        card.appendChild(replyEl);
      });
    }

    card.addEventListener("click", function () {
      setActiveComment(comment, true);
    });

    return card;
  }

  function clearHighlights() {
    if (HAS_CUSTOM_HIGHLIGHT_API) {
      window.CSS.highlights.delete(HIGHLIGHT_KEY_OPEN);
      window.CSS.highlights.delete(HIGHLIGHT_KEY_RESOLVED);
    }
    document.querySelectorAll(".review-line-selected, .review-line-resolved").forEach((node) => {
      node.classList.remove("review-line-selected");
      node.classList.remove("review-line-resolved");
      node.removeAttribute("data-review-comment-ids");
    });
  }

  function clearActiveComment() {
    activeCommentId = null;
    clearHighlights();
    if (!panel) {
      return;
    }
    panel.querySelectorAll(".review-comment.active").forEach((node) => {
      node.classList.remove("active");
    });
  }

  function setActiveComment(comment, scrollToTarget) {
    if (!comment) {
      clearActiveComment();
      return;
    }
    activeCommentId = Number(comment.id);
    clearHighlights();
    panel.querySelectorAll(".review-comment.active").forEach((node) => {
      node.classList.remove("active");
    });

    const card = panel.querySelector(`#review-comment-${comment.id}`);
    if (card) {
      card.classList.add("active");
    }

    highlightComment(comment);

    if (scrollToTarget) {
      const selectedRange = deserializeRange(comment.selection);
      if (selectedRange) {
        const rect = selectedRange.getBoundingClientRect();
        if (rect && Number.isFinite(rect.top) && Number.isFinite(rect.height)) {
          const targetY = window.scrollY + rect.top - Math.max(120, window.innerHeight * 0.35);
          window.scrollTo({ top: Math.max(0, targetY), behavior: "smooth" });
          return;
        }
      }
      const line = Number(comment.line_start);
      if (Number.isFinite(line)) {
        const lineNode = document.querySelector(`[data-review-line="${line}"]`);
        if (lineNode) {
          lineNode.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    }
  }

  function highlightComment(comment) {
    const selectedRange = deserializeRange(comment.selection);
    if (selectedRange && HAS_CUSTOM_HIGHLIGHT_API) {
      const key = comment.resolved ? HIGHLIGHT_KEY_RESOLVED : HIGHLIGHT_KEY_OPEN;
      window.CSS.highlights.set(key, new window.Highlight(selectedRange));
      return;
    }

    const start = Number(comment.line_start);
    const end = Number(comment.line_end);
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      return;
    }
    for (let line = start; line <= end; line += 1) {
      const node = document.querySelector(`[data-review-line="${line}"]`);
      if (!node) {
        continue;
      }
      if (comment.resolved) {
        node.classList.add("review-line-resolved");
      } else {
        node.classList.add("review-line-selected");
      }
      node.setAttribute("data-review-comment-ids", String(comment.id));
      if (!node.id) {
        node.id = `review-line-comment-${comment.id}`;
      }
    }
  }

  async function toggleResolved(commentId, resolved) {
    try {
      await api(`/api/comments/${commentId}/resolve`, { resolved: resolved });
      await refreshComments();
    } catch (err) {
      console.error(err);
      window.alert("Could not update comment status.");
    }
  }

  async function replyTo(commentId) {
    const reviewer = ensureReviewerName(true, function () {
      return replyTo(commentId);
    });
    if (!reviewer) {
      return;
    }
    const body = window.prompt(`Add reply\nPosting as "${reviewer}".`);
    if (!body || !body.trim()) {
      return;
    }
    try {
      await api(`/api/comments/${commentId}/reply`, {
        reviewer: reviewer,
        body: body.trim(),
      });
      await refreshComments();
    } catch (err) {
      console.error(err);
      window.alert("Could not create reply.");
    }
  }

  async function api(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    const data = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      throw new Error(data.error || "request failed");
    }
    return data;
  }

  function jumpToCommentIfRequested(comments) {
    const match = window.location.hash.match(/^#review-comment-(\d+)$/);
    if (!match) {
      return false;
    }
    const commentId = Number(match[1]);
    const comment = comments.find((item) => Number(item.id) === commentId);
    if (!comment) {
      return false;
    }
    setPanelVisible(true);
    setActiveComment(comment, true);
    const card = document.getElementById(`review-comment-${commentId}`);
    if (card) {
      card.scrollIntoView({ block: "center" });
    }
    return true;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
