/**
 * KuroTrack Callback Widget — виджет обратного звонка.
 *
 * Подключение:
 * <script src="https://your-server/callback-widget.js"
 *   data-api="https://your-server/api/v1"
 *   data-key="PROJECT_API_KEY"
 *   data-position="right"
 *   data-color="#6366f1">
 * </script>
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var API_URL = script.getAttribute("data-api");
  var API_KEY = script.getAttribute("data-key");
  var POSITION = script.getAttribute("data-position") || "right";
  var COLOR = script.getAttribute("data-color") || "#6366f1";

  if (!API_URL || !API_KEY) return;

  // --- Styles ---
  var css = [
    ".kt-cb-btn{position:fixed;bottom:24px;" + POSITION + ":24px;width:56px;height:56px;border-radius:50%;background:" + COLOR + ";color:#fff;border:none;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.3);font-size:24px;z-index:99998;transition:transform .2s}",
    ".kt-cb-btn:hover{transform:scale(1.1)}",
    ".kt-cb-popup{position:fixed;bottom:90px;" + POSITION + ":24px;width:320px;background:#1a1d27;border:1px solid #2e3040;border-radius:12px;padding:24px;z-index:99999;box-shadow:0 8px 32px rgba(0,0,0,.5);font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:none}",
    ".kt-cb-popup.active{display:block}",
    ".kt-cb-popup h3{color:#e4e4e7;margin:0 0 4px;font-size:16px}",
    ".kt-cb-popup p{color:#8b8d97;margin:0 0 16px;font-size:13px}",
    ".kt-cb-popup input{width:100%;padding:10px 14px;background:#252836;border:1px solid #2e3040;border-radius:8px;color:#e4e4e7;font-size:14px;margin-bottom:12px;box-sizing:border-box}",
    ".kt-cb-popup input:focus{outline:none;border-color:" + COLOR + "}",
    ".kt-cb-popup button.kt-submit{width:100%;padding:12px;background:" + COLOR + ";color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;transition:opacity .15s}",
    ".kt-cb-popup button.kt-submit:hover{opacity:.9}",
    ".kt-cb-popup .kt-success{text-align:center;color:#22c55e;font-size:14px}",
    ".kt-cb-popup .kt-error{color:#ef4444;font-size:12px;margin-bottom:8px}",
    ".kt-cb-close{position:absolute;top:8px;right:12px;background:none;border:none;color:#8b8d97;font-size:18px;cursor:pointer}",
  ].join("\n");

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  // --- Button ---
  var btn = document.createElement("button");
  btn.className = "kt-cb-btn";
  btn.innerHTML = "&#128222;";
  btn.title = "Request callback";
  document.body.appendChild(btn);

  // --- Popup ---
  var popup = document.createElement("div");
  popup.className = "kt-cb-popup";
  popup.innerHTML = [
    '<button class="kt-cb-close">&times;</button>',
    "<h3>Callback</h3>",
    "<p>Enter your number and we'll call you back</p>",
    '<div class="kt-error" style="display:none"></div>',
    '<input type="tel" placeholder="+7 (___) ___-__-__" class="kt-cb-phone"/>',
    '<input type="text" placeholder="Your name (optional)" class="kt-cb-name"/>',
    '<button class="kt-submit">Call me back</button>',
    '<div class="kt-success" style="display:none">We\'ll call you shortly!</div>',
  ].join("");
  document.body.appendChild(popup);

  var phoneInput = popup.querySelector(".kt-cb-phone");
  var nameInput = popup.querySelector(".kt-cb-name");
  var submitBtn = popup.querySelector(".kt-submit");
  var errorDiv = popup.querySelector(".kt-error");
  var successDiv = popup.querySelector(".kt-success");
  var closeBtn = popup.querySelector(".kt-cb-close");

  btn.onclick = function () {
    popup.classList.toggle("active");
  };

  closeBtn.onclick = function () {
    popup.classList.remove("active");
  };

  submitBtn.onclick = function () {
    var phone = phoneInput.value.replace(/[^\d+]/g, "");
    if (phone.length < 10) {
      errorDiv.style.display = "block";
      errorDiv.textContent = "Enter a valid phone number";
      return;
    }

    errorDiv.style.display = "none";
    submitBtn.disabled = true;
    submitBtn.textContent = "Calling...";

    var xhr = new XMLHttpRequest();
    xhr.open("POST", API_URL + "/callback/request", true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("X-Api-Key", API_KEY);
    xhr.onreadystatechange = function () {
      if (xhr.readyState === 4) {
        if (xhr.status >= 200 && xhr.status < 300) {
          submitBtn.style.display = "none";
          phoneInput.style.display = "none";
          nameInput.style.display = "none";
          successDiv.style.display = "block";
          setTimeout(function () {
            popup.classList.remove("active");
            // Reset
            submitBtn.style.display = "";
            phoneInput.style.display = "";
            nameInput.style.display = "";
            successDiv.style.display = "none";
            submitBtn.disabled = false;
            submitBtn.textContent = "Call me back";
            phoneInput.value = "";
            nameInput.value = "";
          }, 3000);
        } else {
          errorDiv.style.display = "block";
          errorDiv.textContent = "Failed to request callback";
          submitBtn.disabled = false;
          submitBtn.textContent = "Call me back";
        }
      }
    };
    xhr.send(JSON.stringify({ phone: phone, name: nameInput.value || null }));
  };
})();
