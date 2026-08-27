(function () {
  const ctx = window.REVIEW_CONTEXT;
  if (!ctx || !ctx.buildId || !ctx.pagePath) {
    return;
  }

  const REVIEWER_KEY = "docs-review-reviewer";
  const PANEL_VISIBLE_KEY = "docs-review-panel-visible";
  // True when this script is loaded directly on the standalone changed-pages
  // page (viewed by URL, not embedded in the preview page's modal) — there's
  // no rendered prose here, and the diff page's own script already owns
  // selection-to-comment, so this widget only needs to render the list.
  const DIFF_MODE = ctx.mode === "diff";
  // ctx.pagePath is temporarily overridden while the diff modal has a
  // different file open (see __reviewSetActivePage) and restored to this
  // once the modal closes — the widget is a single instance either way.
  const basePagePath = ctx.pagePath;
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
  let diffModalOverlay = null;
  let diffModalIframe = null;
  let lastHandledHash = null;
  let commentComposer = null;

  if (!DIFF_MODE) {
    assignLineNumbers();
  }
  panel = createPanel();
  try {
    if (window.localStorage.getItem(PANEL_VISIBLE_KEY) === "0") {
      setPanelVisible(false);
    }
  } catch (_) {}
  refreshComments();

  // The changed-pages diff modal calls this (as window.parent.__reviewSetActivePage)
  // whenever the user picks a different file in its sidebar — there's no full
  // reload, so nothing else tells this widget the focused page changed. It's
  // restored to basePagePath when the modal closes, see closeModal().
  window.__reviewSetActivePage = function (pagePath) {
    ctx.pagePath = pagePath;
    // Don't force-clear activeCommentId here — renderComments() (called via
    // refreshComments() below) already keeps it active if that comment is in
    // the newly-fetched list, and clears it otherwise. Clearing unconditionally
    // here would wipe out the very comment a click just activated, since
    // opening/refreshing the diff modal always routes back through this.
    refreshComments();
  };

  // In DIFF_MODE, the diff page's own inline script already owns
  // selecting diff text to add a comment (it knows about diff row keys,
  // which this widget doesn't) — don't also attach a prose-oriented
  // selection listener that would just misfire over diff table content.
  if (!DIFF_MODE) {
    document.addEventListener("mouseup", onSelectionComplete);
    document.addEventListener("keyup", onSelectionComplete);
  }
  document.addEventListener("click", onDocumentClick);
  window.addEventListener("scroll", onViewportChanged, true);
  window.addEventListener("resize", onViewportChanged);
  // A link to a comment on the page already loaded here only changes the
  // hash (no full reload), so re-run the jump-to-comment logic by hand —
  // otherwise it only ever fires once, on the initial page load.
  window.addEventListener("hashchange", function () {
    renderComments(commentsCache);
  });

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
      floatingButton.addEventListener("click", function () {
        if (!pendingSelection) {
          return;
        }
        const selectionData = clonePendingSelection(pendingSelection);
        const rect = pendingSelection.rect;
        hideFloatingButton();
        showCommentComposer(rect, function (reviewer, body) {
          setReviewerName(reviewer);
          addCommentForSelection(selectionData, reviewer, body);
        });
      });
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

  function removeCommentComposer() {
    if (commentComposer) {
      commentComposer.remove();
      commentComposer = null;
    }
  }

  function showCommentComposer(rect, onSubmit) {
    removeCommentComposer();
    commentComposer = document.createElement("div");
    commentComposer.className = "review-comment-composer";
    const margin = 10;
    const composerWidth = 260;
    const composerHeight = 160;
    let left = rect.left;
    let top = rect.bottom + 8;
    if (left + composerWidth > window.innerWidth - margin) {
      left = window.innerWidth - composerWidth - margin;
    }
    if (left < margin) {
      left = margin;
    }
    if (top + composerHeight > window.innerHeight - margin) {
      top = Math.max(margin, rect.top - composerHeight - 8);
    }
    commentComposer.style.left = `${Math.round(left)}px`;
    commentComposer.style.top = `${Math.round(top)}px`;
    commentComposer.innerHTML = `
      <input type="text" class="review-composer-reviewer" placeholder="Your name" value="${escapeHtml(getReviewerName())}">
      <textarea class="review-composer-body" placeholder="Leave a comment" rows="3"></textarea>
      <div class="review-composer-error" hidden></div>
      <div class="review-composer-actions">
        <button type="button" class="review-composer-cancel">Cancel</button>
        <button type="button" class="review-composer-submit">Comment</button>
      </div>
    `;
    document.body.appendChild(commentComposer);
    const reviewerInput = commentComposer.querySelector(".review-composer-reviewer");
    const textarea = commentComposer.querySelector(".review-composer-body");
    const errorEl = commentComposer.querySelector(".review-composer-error");
    if (getReviewerName()) {
      textarea.focus();
    } else {
      reviewerInput.focus();
    }
    commentComposer.querySelector(".review-composer-cancel").addEventListener("click", function () {
      removeCommentComposer();
      const selection = window.getSelection();
      if (selection) {
        selection.removeAllRanges();
      }
      hideFloatingButton();
    });
    commentComposer.querySelector(".review-composer-submit").addEventListener("click", function () {
      const reviewer = reviewerInput.value.trim();
      const body = textarea.value.trim();
      if (!reviewer) {
        errorEl.textContent = "Enter your name to comment.";
        errorEl.hidden = false;
        reviewerInput.focus();
        return;
      }
      if (!body) {
        errorEl.textContent = "Comment text is required.";
        errorEl.hidden = false;
        textarea.focus();
        return;
      }
      removeCommentComposer();
      onSubmit(reviewer, body);
    });
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
    return trimmed;
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
    try { window.localStorage.setItem(PANEL_VISIBLE_KEY, visible ? "1" : "0"); } catch (_) {}
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
            ${ctx.changedPages && ctx.changedPages.length ? `<button type="button" class="review-icon-button" id="review-diff-btn" title="Diff and changed pages" aria-label="Diff and changed pages">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M12 3v18"></path><path d="M5 10l-3 3 3 3"></path><path d="M19 10l3 3-3 3"></path>
              </svg>
            </button>` : ""}
            <button type="button" class="review-icon-button" id="review-view-all-comments" title="View all comments" aria-label="View all comments">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M21 12a8.5 8.5 0 0 1-8.5 8.5H6l-3 3v-6.5A8.5 8.5 0 1 1 21 12z"></path>
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

    const toggleButton = aside.querySelector("#review-toggle-resolved");
    updateResolvedToggleLabel(toggleButton);
    if (toggleButton) {
      toggleButton.addEventListener("click", function () {
        showResolved = !showResolved;
        updateResolvedToggleLabel(toggleButton);
        renderComments(commentsCache);
      });
    }

    var diffBtn = aside.querySelector("#review-diff-btn");
    if (diffBtn) {
      diffBtn.addEventListener("click", function () { openDiffModal(); });
    }

    var viewAllCommentsBtn = aside.querySelector("#review-view-all-comments");
    if (viewAllCommentsBtn) {
      viewAllCommentsBtn.addEventListener("click", openCommentsModal);
    }

    return aside;
  }

  function ensureModal() {
    if (diffModalOverlay) {
      return;
    }
    diffModalOverlay = document.createElement("div");
    diffModalOverlay.className = "review-diff-modal-overlay";
    diffModalOverlay.hidden = true;
    diffModalOverlay.innerHTML = `
      <div class="review-diff-modal">
        <div class="review-diff-modal-head">
          <span class="review-diff-modal-title"></span>
          <button type="button" class="review-diff-modal-close" aria-label="Close">&times;</button>
        </div>
        <iframe class="review-diff-modal-iframe" title="Preview modal"></iframe>
      </div>
    `;
    document.body.appendChild(diffModalOverlay);
    diffModalIframe = diffModalOverlay.querySelector(".review-diff-modal-iframe");
    diffModalOverlay.querySelector(".review-diff-modal-close").addEventListener("click", closeModal);
    diffModalOverlay.addEventListener("click", function (event) {
      if (event.target === diffModalOverlay) {
        closeModal();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && diffModalOverlay && !diffModalOverlay.hidden) {
        closeModal();
      }
    });
  }

  function openModal(title, url) {
    ensureModal();
    diffModalOverlay.querySelector(".review-diff-modal-title").textContent = title;
    diffModalIframe.title = title;
    diffModalIframe.src = url;
    diffModalOverlay.hidden = false;
  }

  function closeModal() {
    if (diffModalOverlay) {
      diffModalOverlay.hidden = true;
    }
    if (diffModalIframe) {
      diffModalIframe.src = "about:blank";
    }
    // The diff modal may have pointed the widget at whichever file the user
    // was browsing in its sidebar — go back to showing comments for the page
    // actually being previewed now that it's closed. No-op for the comments
    // modal, which never touches ctx.pagePath. (renderComments(), called via
    // refreshComments(), decides on its own whether activeCommentId survives —
    // see the comment in __reviewSetActivePage.)
    if (ctx.pagePath !== basePagePath) {
      ctx.pagePath = basePagePath;
      refreshComments();
    }
  }

  function pushHighlightsToDiffModal() {
    if (!diffModalOverlay || diffModalOverlay.hidden || !diffModalIframe) {
      return;
    }
    try {
      var win = diffModalIframe.contentWindow;
      if (win && typeof win.__applyCommentHighlights === "function") {
        win.__applyCommentHighlights(commentsCache);
      }
    } catch (_) {
      // Cross-frame access can throw if the iframe hasn't finished loading yet.
    }
  }

  // Exposed so the same-origin iframes we open (changed-pages, comments) can
  // close us before navigating — otherwise a link to a page/anchor that's
  // already loaded here doesn't trigger a full reload, and this fixed,
  // full-screen overlay is left covering the page with nothing visibly
  // happening.
  window.__reviewCloseModal = closeModal;

  function openDiffModal(highlightCommentId) {
    var url = "/previews/" + encodeURIComponent(String(ctx.buildId)) + "/changed-pages?embed=1&select="
      + encodeURIComponent(ctx.pagePath);
    if (highlightCommentId) {
      url += "&highlight=" + encodeURIComponent(String(highlightCommentId));
    }
    openModal("Changed pages", url);
  }

  function openCommentsModal() {
    var url = "/comments?embed=1&build_id=" + encodeURIComponent(String(ctx.buildId));
    openModal("Comments", url);
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

  async function addCommentForSelection(selectionData, reviewer, body) {
    if (!selectionData || !reviewer || !body) {
      return;
    }
    const payload = {
      build_id: ctx.buildId,
      page_path: ctx.pagePath,
      selected_text: selectionData.selectedText,
      body: body,
      reviewer: reviewer,
      line_start: selectionData.lineStart,
      line_end: selectionData.lineEnd,
      selection: selectionData.selection,
    };
    try {
      await api("/api/comments", payload);
      const selection = window.getSelection();
      if (selection) {
        selection.removeAllRanges();
      }
      await refreshComments();
    } catch (err) {
      console.error(err);
      window.alert("Could not create comment.");
    }
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
      pushHighlightsToDiffModal();
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
      ${comment.kind === "diff" ? '<span class="review-comment-kind-badge">diff</span>' : ""}
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
    replyButton.addEventListener("click", function (event) {
      event.stopPropagation();
      replyTo(comment.id, replyButton.getBoundingClientRect());
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
      if (DIFF_MODE) {
        // We ARE the diff page here (standalone view, no modal involved) —
        // reveal directly via the diff-rendering script's own hook.
        setActiveComment(comment, false);
        if (comment.kind === "diff" && window.__revealAndScrollToDiffKey) {
          window.__revealAndScrollToDiffKey(comment.diff_start_key) ||
            window.__revealAndScrollToDiffKey(comment.diff_end_key);
        } else if (comment.kind !== "diff") {
          openPreviewForComment(comment);
        }
        return;
      }
      // Whenever the diff modal is open, any comment currently listed here
      // belongs to whichever page it's showing (ctx.pagePath tracks that) —
      // comparing to basePagePath would wrongly say "not showing this page"
      // for the common case of opening the modal on the page you're on.
      const modalOpen = diffModalOverlay && !diffModalOverlay.hidden;
      if (comment.kind === "diff") {
        // No comment.selection/line_start to scroll to in the rendered page for
        // a diff comment, so setActiveComment just applies the "active" blue
        // outline here — the actual reveal happens in the diff table below.
        setActiveComment(comment, false);
        if (modalOpen) {
          revealInDiffModal(comment);
        } else {
          openDiffModal(comment.id);
        }
      } else if (ctx.pagePath !== basePagePath) {
        openPreviewForComment(comment);
      } else {
        // The modal (if open) covers the whole screen, so a preview comment's
        // scroll-and-highlight in the page underneath would otherwise happen
        // out of view.
        closeModal();
        setActiveComment(comment, true);
      }
    });

    return card;
  }

  function buildUrlFor(pagePath) {
    return "/" + ctx.buildTag + pagePath;
  }

  function openPreviewForComment(comment) {
    closeModal();
    window.location.href = buildUrlFor(String(comment.page_path)) + "#review-comment-" + comment.id;
  }

  // The diff table lives inside the modal's iframe, not this document, so
  // revealing a row means reaching into it — its own script exposes this,
  // see __revealAndScrollToDiffKey in the changed-pages page.
  function revealInDiffModal(comment) {
    if (!diffModalIframe) {
      return;
    }
    try {
      var win = diffModalIframe.contentWindow;
      if (win && typeof win.__revealAndScrollToDiffKey === "function") {
        win.__revealAndScrollToDiffKey(comment.diff_start_key) ||
          win.__revealAndScrollToDiffKey(comment.diff_end_key);
      }
    } catch (_) {}
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

  function replyTo(commentId, rect) {
    showCommentComposer(rect, function (reviewer, body) {
      setReviewerName(reviewer);
      postReply(commentId, reviewer, body);
    });
  }

  async function postReply(commentId, reviewer, body) {
    try {
      await api(`/api/comments/${commentId}/reply`, {
        reviewer: reviewer,
        body: body,
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
    // Handle a given hash at most once per renderComments() call cycle — it
    // re-runs after unrelated actions (toggling "show resolved", posting a
    // reply, a diff modal reporting its active page, ...), and without this
    // guard the one-time "jump to this comment" action would instead fire on
    // every re-render, permanently overriding whatever comment was actually
    // last clicked with whichever one the URL hash happens to name.
    const alreadyHandled = window.location.hash === lastHandledHash;

    const diffMatch = window.location.hash.match(/^#review-diff-comment-(\d+)$/);
    if (diffMatch) {
      if (alreadyHandled) {
        return false;
      }
      const diffCommentId = Number(diffMatch[1]);
      const diffComment = comments.find((item) => Number(item.id) === diffCommentId);
      if (!diffComment) {
        return false;
      }
      lastHandledHash = window.location.hash;
      setPanelVisible(true);
      openDiffModal(diffCommentId);
      return true;
    }

    const match = window.location.hash.match(/^#review-comment-(\d+)$/);
    if (!match || alreadyHandled) {
      return false;
    }
    const commentId = Number(match[1]);
    const comment = comments.find((item) => Number(item.id) === commentId);
    if (!comment) {
      return false;
    }
    lastHandledHash = window.location.hash;
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
