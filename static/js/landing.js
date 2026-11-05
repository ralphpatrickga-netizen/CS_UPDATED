/* BarangayConnect — landing page behaviour.
   Plain JS, no dependencies. Everything degrades gracefully: without JS the
   page is fully visible (the hidden "pre-reveal" states are scoped to `.js`),
   and with prefers-reduced-motion the animated bits render in a settled state. */
(function () {
  "use strict";

  var root = document.documentElement;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (sel, ctx) { return (ctx || document).querySelector(sel); };
  var $$ = function (sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); };

  /* ---------------------------------------------------------- hero entrance */
  requestAnimationFrame(function () {
    requestAnimationFrame(function () { root.classList.add("is-ready"); });
  });

  /* ---------------------------------------------------------- nav + progress */
  var nav = $("#lp-nav");
  var bar = $("#lp-progress-bar");
  var burger = $("#lp-burger");
  var linksWrap = $("#lp-links");
  var navLinks = $$("a[href^='#']", linksWrap);   // in-page anchors only
  var sections = navLinks
    .map(function (a) { return $(a.getAttribute("href")); })
    .filter(Boolean);

  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var y = window.scrollY || window.pageYOffset;
      var max = document.documentElement.scrollHeight - window.innerHeight;

      nav.classList.toggle("is-solid", y > 40 || nav.classList.contains("menu-open"));
      if (bar && max > 0) bar.style.transform = "scaleX(" + Math.min(1, y / max) + ")";

      // current section in the nav
      var probe = y + window.innerHeight * 0.35;
      var current = null;
      sections.forEach(function (s) { if (s.offsetTop <= probe) current = s.id; });
      navLinks.forEach(function (a) {
        a.classList.toggle("is-active", a.getAttribute("href") === "#" + current);
      });

      updateTimeline();
      ticking = false;
    });
  }

  function setMenu(open) {
    linksWrap.classList.toggle("is-open", open);
    nav.classList.toggle("menu-open", open);
    burger.setAttribute("aria-expanded", String(open));
    burger.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    nav.classList.toggle("is-solid", open || (window.scrollY || 0) > 40);
  }
  burger.addEventListener("click", function () {
    setMenu(burger.getAttribute("aria-expanded") !== "true");
  });
  $$("a", linksWrap).forEach(function (a) { a.addEventListener("click", function () { setMenu(false); }); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
  window.addEventListener("resize", function () { if (window.innerWidth > 860) setMenu(false); });

  /* ---------------------------------------------------------- scroll reveals */
  var revealEls = $$("[data-reveal], [data-stagger]");
  if ("IntersectionObserver" in window && !reduceMotion) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          en.target.classList.add("is-in");
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.18, rootMargin: "0px 0px -6% 0px" });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("is-in"); });
  }

  /* ---------------------------------------------------------- "how it works" timeline
     The line fills as you scroll and each step lights up when it passes
     the middle of the screen. */
  var timeline = $("#lp-timeline");
  var fill = $("#lp-timeline-fill");
  var tlItems = $$(".lp-tl-item", timeline);

  function updateTimeline() {
    if (!timeline) return;
    var rect = timeline.getBoundingClientRect();
    var vh = window.innerHeight;
    var focus = vh * 0.55;
    var progress = (focus - rect.top) / rect.height;
    progress = Math.max(0, Math.min(1, progress));
    if (fill) fill.style.transform = "scaleY(" + progress + ")";
    tlItems.forEach(function (item) {
      var node = $(".lp-tl-n", item).getBoundingClientRect();
      item.classList.toggle("is-lit", node.top + node.height / 2 < focus);
    });
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  onScroll();

  /* ---------------------------------------------------------- hero request tracker
     Walks an example request through the real status flow, then loops.
     Paused while the hero is off-screen so it doesn't run in the background. */
  var statusEl = $("#lp-status");
  var steps = $$("#lp-steps li");
  var toast = $("#lp-toast");
  var toastText = $("#lp-toast-text");
  var tracker = $("#lp-tracker");

  var STATES = [
    { label: "Submitted",    msg: "Your request is now Submitted." },
    { label: "Under Review", msg: "The Secretary is reviewing your request." },
    { label: "Processing",   msg: "Your certificate is being prepared." },
    { label: "Approved",     msg: "Approved. Your claim slip is ready." },
    { label: "Completed",    msg: "Completed. Thank you for claiming!" }
  ];

  function showState(i, animate) {
    statusEl.textContent = STATES[i].label;
    statusEl.setAttribute("data-state", i);
    steps.forEach(function (li, idx) {
      li.classList.toggle("is-done", idx < i || (i === STATES.length - 1 && idx === i));
      li.classList.toggle("is-current", idx === i && i !== STATES.length - 1);
    });
    toastText.textContent = STATES[i].msg;
    if (animate) {
      toast.classList.remove("is-pop");
      void toast.offsetWidth;            // restart the animation
      toast.classList.add("is-pop");
    }
  }

  var trackerTimer = null;
  var trackerIndex = 0;
  var heroVisible = true;

  function tick() {
    trackerIndex += 1;
    if (trackerIndex >= STATES.length) {
      // hold on "Completed", then start over
      trackerTimer = setTimeout(function () {
        trackerIndex = 0;
        showState(0, true);
        trackerTimer = setTimeout(tick, 2000);
      }, 2800);
      return;
    }
    showState(trackerIndex, true);
    trackerTimer = setTimeout(tick, 2000);
  }

  function startTracker() {
    if (trackerTimer || reduceMotion) return;
    trackerTimer = setTimeout(tick, 1900);
  }
  function stopTracker() { clearTimeout(trackerTimer); trackerTimer = null; }

  if (tracker) {
    if (reduceMotion) {
      showState(3, false);              // static, informative snapshot
    } else {
      showState(0, false);
      setTimeout(startTracker, 1200);   // let the entrance finish first
      if ("IntersectionObserver" in window) {
        new IntersectionObserver(function (entries) {
          heroVisible = entries[0].isIntersecting;
          if (heroVisible) startTracker(); else stopTracker();
        }, { threshold: 0.15 }).observe($("#hero"));
      }
    }
  }

})();
