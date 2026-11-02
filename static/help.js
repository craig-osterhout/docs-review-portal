(function () {
  "use strict";

  // A Copy button on every command, so it can be pasted straight into a terminal.
  document.querySelectorAll("pre[data-copy]").forEach(function (block) {
    const wrapper = document.createElement("div");
    wrapper.className = "help-code";
    block.parentNode.insertBefore(wrapper, block);
    wrapper.appendChild(block);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "help-copy";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy this command");
    wrapper.appendChild(button);

    function done(label) {
      button.textContent = label;
      window.setTimeout(function () { button.textContent = "Copy"; }, 1500);
    }

    button.addEventListener("click", function () {
      const text = block.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done("Copied"); }, function () { fallback(text); });
      } else {
        fallback(text);
      }
    });

    // Used where the clipboard API isn't available, such as a page served without https.
    function fallback(text) {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      area.remove();
      done(ok ? "Copied" : "Press Ctrl+C");
    }
  });

  // Highlights the section being read in the table of contents.
  const links = Array.prototype.slice.call(document.querySelectorAll(".help-toc a"));
  const byId = {};
  links.forEach(function (link) { byId[link.getAttribute("href").slice(1)] = link; });
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting && byId[entry.target.id]) {
          links.forEach(function (link) { link.classList.remove("is-current"); });
          byId[entry.target.id].classList.add("is-current");
        }
      });
    }, { rootMargin: "0px 0px -70% 0px" });
    document.querySelectorAll(".help-section").forEach(function (section) { observer.observe(section); });
  }
})();
