(function () {
  'use strict';

  var CHUNK_SIZE = 20 * 1024 * 1024; // 20 MiB — matches server/script limit

  var dialog = document.getElementById('upload-dialog');
  var form = document.getElementById('upload-form');
  if (!dialog || !form) return;

  // Tab switching.
  var tabBtns = dialog.querySelectorAll('.tab-btn');
  tabBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      tabBtns.forEach(function (b) {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      var target = btn.getAttribute('data-tab');
      dialog.querySelectorAll('.tab-panel').forEach(function (panel) {
        panel.hidden = panel.id !== 'tab-' + target;
      });
    });
  });

  // Open the dialog.
  var openBtn = document.getElementById('open-upload-dialog');
  if (openBtn) {
    openBtn.addEventListener('click', function () {
      dialog.showModal();
    });
  }

  // Close button inside the dialog.
  var closeBtn = document.getElementById('close-upload-dialog');
  if (closeBtn) {
    closeBtn.addEventListener('click', function () {
      dialog.close();
    });
  }

  // Close when clicking the backdrop (outside the dialog box).
  dialog.addEventListener('click', function (e) {
    if (e.target === dialog) dialog.close();
  });

  // Reset tabs and progress UI whenever the dialog is closed.
  dialog.addEventListener('close', function () {
    tabBtns.forEach(function (b) {
      var isUpload = b.getAttribute('data-tab') === 'upload';
      b.classList.toggle('active', isUpload);
      b.setAttribute('aria-selected', isUpload ? 'true' : 'false');
    });
    dialog.querySelectorAll('.tab-panel').forEach(function (panel) {
      panel.hidden = panel.id !== 'tab-upload';
    });
    var progressEl = document.getElementById('upload-progress');
    var bar = document.getElementById('upload-bar');
    var statusEl = document.getElementById('upload-status');
    var submitBtn = form.querySelector('button[type="submit"]');
    if (progressEl) progressEl.hidden = true;
    if (bar) { bar.style.width = '0%'; bar.className = 'upload-bar'; }
    if (statusEl) statusEl.textContent = '';
    if (submitBtn) submitBtn.disabled = false;
    form.reset();
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();

    var name = form.elements['name'].value.trim();
    var file = form.elements['archive'].files[0];
    var rewriteHost = (form.elements['rewrite_host'].value || '').trim();

    if (!file) return;

    var progressEl = document.getElementById('upload-progress');
    var bar = document.getElementById('upload-bar');
    var statusEl = document.getElementById('upload-status');
    var submitBtn = form.querySelector('button[type="submit"]');

    progressEl.hidden = false;
    submitBtn.disabled = true;

    function setBar(pct, text, state) {
      bar.style.width = Math.min(100, Math.max(0, pct)) + '%';
      bar.className = 'upload-bar' + (state ? ' ' + state : '');
      if (text !== undefined) statusEl.textContent = text;
    }

    doUpload(file, name, rewriteHost, setBar).catch(function (err) {
      setBar(0, 'Error: ' + (err && err.message ? err.message : String(err)), 'error');
      submitBtn.disabled = false;
    });
  });

  function doUpload(file, name, rewriteHost, setBar) {
    var totalChunks = Math.ceil(file.size / CHUNK_SIZE);
    var baseParams = new URLSearchParams({ name: name });
    if (rewriteHost) baseParams.set('rewrite_host', rewriteHost);

    if (totalChunks <= 1) {
      setBar(10, 'Uploading…');
      return fetch('/api/builds/upload?' + baseParams, {
        method: 'POST',
        headers: { 'Content-Type': 'application/gzip' },
        body: file,
      }).then(function (resp) {
        return streamProgress(resp, setBar, 20, 100);
      });
    }

    // Chunked upload — upload each slice sequentially, stream the last response.
    var i = 0;
    function nextChunk() {
      if (i >= totalChunks) return Promise.resolve();
      var idx = i++;
      var isLast = idx === totalChunks - 1;
      var chunk = file.slice(idx * CHUNK_SIZE, (idx + 1) * CHUNK_SIZE);
      var params = new URLSearchParams(baseParams);
      params.set('chunk', String(idx));
      params.set('chunks', String(totalChunks));

      var uploadPct = Math.round((idx / totalChunks) * 60);
      setBar(uploadPct, 'Uploading part ' + (idx + 1) + ' of ' + totalChunks + '…');

      return fetch('/api/builds/upload?' + params, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: chunk,
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (t) {
            throw new Error(t.trim() || 'Upload failed (' + resp.status + ')');
          });
        }
        if (!isLast) {
          return resp.json().then(function () { return nextChunk(); });
        }
        return streamProgress(resp, setBar, 60, 100);
      });
    }
    return nextChunk();
  }

  function streamProgress(resp, setBar, startPct, endPct) {
    if (!resp.ok) {
      return resp.text().then(function (t) {
        throw new Error(t.trim() || 'Server error (' + resp.status + ')');
      });
    }

    var midPct = Math.round((startPct + endPct) / 2);
    setBar(startPct, 'Processing on server…');

    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = '';

    function read() {
      return reader.read().then(function (result) {
        if (result.done) return;
        buf += decoder.decode(result.value, { stream: true });
        var lines = buf.split('\n');
        buf = lines.pop(); // keep partial last line
        for (var j = 0; j < lines.length; j++) {
          var line = lines[j];
          if (line.indexOf('status: ') === 0) {
            setBar(midPct, line.slice(8));
          } else if (line.indexOf('result: ') === 0) {
            setBar(100, 'Upload complete!', 'success');
            setTimeout(function () { dialog.close(); location.reload(); }, 1500);
            return;
          } else if (line.indexOf('error: ') === 0) {
            throw new Error(line.slice(7));
          }
        }
        return read();
      });
    }
    return read();
  }
})();
