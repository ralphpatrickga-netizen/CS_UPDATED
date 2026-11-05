(function () {
  "use strict";

  var countdown = document.querySelector(".concern-reset-countdown");
  if (!countdown) return;

  var value = countdown.querySelector(".countdown-value");
  var seconds = Number(countdown.getAttribute("data-countdown-seconds"));
  if (!value || !Number.isFinite(seconds)) return;

  var deadline = Date.now() + Math.max(0, seconds) * 1000;

  function updateCountdown() {
    var remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    var hours = Math.floor(remaining / 3600);
    var minutes = Math.floor((remaining % 3600) / 60);
    var secs = remaining % 60;

    if (remaining === 0) {
      value.textContent = "Reset reached — refresh this page";
      window.clearInterval(timer);
      return;
    }

    value.textContent = hours + "h " + minutes + "m " + secs + "s";
  }

  updateCountdown();
  var timer = window.setInterval(updateCountdown, 1000);
})();
