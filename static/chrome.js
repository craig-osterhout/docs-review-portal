(function () {
  "use strict";

  var KEY = "review-portal-theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem(KEY, theme);
    } catch (_) {}
    var toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }
  }

  function init() {
    var toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-pressed", currentTheme() === "dark" ? "true" : "false");
      toggle.addEventListener("click", function () {
        applyTheme(currentTheme() === "dark" ? "light" : "dark");
      });
    }
    // On small screens the navigation is a menu behind the hamburger button.
    var navToggle = document.querySelector("[data-nav-toggle]");
    var nav = document.getElementById("site-nav");
    function setMenu(open, returnFocus) {
      if (!navToggle || !nav) {
        return;
      }
      nav.classList.toggle("is-open", open);
      navToggle.classList.toggle("is-open", open);
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open && returnFocus) {
        navToggle.focus();
      }
    }
    if (navToggle && nav) {
      navToggle.addEventListener("click", function () {
        setMenu(!nav.classList.contains("is-open"), false);
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && nav.classList.contains("is-open")) {
          setMenu(false, true);
        }
      });
      document.addEventListener("click", function (event) {
        if (nav.classList.contains("is-open") && !nav.contains(event.target) && !navToggle.contains(event.target)) {
          setMenu(false, false);
        }
      });
      window.addEventListener("resize", function () {
        if (window.innerWidth > 768) {
          setMenu(false, false);
        }
      });
    }

    // Marks the navigation link for the page being viewed.
    var path = window.location.pathname;
    document.querySelectorAll(".nav a").forEach(function (link) {
      var href = link.getAttribute("href");
      if (path === href || path.indexOf(href + "/") === 0) {
        link.setAttribute("aria-current", "page");
      }
    });
  }

  // A change made in another tab, or in a preview's diff window, applies here too.
  window.addEventListener("storage", function (event) {
    if (event.key === KEY && (event.newValue === "light" || event.newValue === "dark")) {
      document.documentElement.setAttribute("data-theme", event.newValue);
      var toggle = document.querySelector("[data-theme-toggle]");
      if (toggle) {
        toggle.setAttribute("aria-pressed", event.newValue === "dark" ? "true" : "false");
      }
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
