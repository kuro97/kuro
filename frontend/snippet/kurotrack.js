/**
 * KuroTrack DNI (Dynamic Number Insertion) — скрипт подмены номеров.
 *
 * Подключение на сайт:
 * <script src="https://your-server/kurotrack.js"
 *   data-api="https://your-server/api/v1"
 *   data-key="PROJECT_API_KEY"
 *   data-selector=".kt-phone">
 * </script>
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var API_URL = script.getAttribute("data-api");
  var API_KEY = script.getAttribute("data-key");
  var SELECTOR = script.getAttribute("data-selector") || ".kt-phone";
  var HEARTBEAT_INTERVAL = 30000; // 30 сек

  // Регекс плейсхолдера: телефон-заглушка где все цифры = нули (Tilda-шаблоны)
  var PLACEHOLDER_RE = /[+]?[78][\s\-]*\(?0{3}\)?[\s\-]*0{3}[\s\-]*0{2}[\s\-]*0{2}/g;

  if (!API_URL || !API_KEY) {
    console.error("[KuroTrack] data-api and data-key attributes required");
    return;
  }

  // --- Утилиты ---

  function getClientId() {
    var id = localStorage.getItem("kt_client_id");
    if (!id) {
      id = "kt_" + Math.random().toString(36).substr(2, 12) + Date.now().toString(36);
      localStorage.setItem("kt_client_id", id);
    }
    return id;
  }

  function getUrlParam(name) {
    var match = RegExp("[?&]" + name + "=([^&]*)").exec(window.location.search);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function getTrafficSource() {
    return {
      client_id: getClientId(),
      source: getUrlParam("utm_source"),
      medium: getUrlParam("utm_medium"),
      campaign: getUrlParam("utm_campaign"),
      keyword: getUrlParam("utm_keyword"),
      content: getUrlParam("utm_content"),
      gclid: getUrlParam("gclid"),
      referrer: document.referrer || null,
      landing_page: window.location.href,
    };
  }

  // --- Подмена номеров в DOM ---

  function replacePhones(newPhone) {
    var elements = document.querySelectorAll(SELECTOR);
    var formatted = formatPhone(newPhone);

    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      el.textContent = formatted;

      // Обновляем href="tel:..." если это ссылка
      if (el.tagName === "A" && el.getAttribute("href")) {
        el.setAttribute("href", "tel:" + newPhone.replace(/[^+\d]/g, ""));
      }

      // Проверяем родительский <a>
      var parent = el.parentElement;
      if (parent && parent.tagName === "A" && parent.getAttribute("href")) {
        var href = parent.getAttribute("href");
        if (href.indexOf("tel:") === 0) {
          parent.setAttribute("href", "tel:" + newPhone.replace(/[^+\d]/g, ""));
        }
      }
    }
  }

  function formatPhone(phone) {
    // +77001234567 → +7 (700) 123-45-67
    var digits = phone.replace(/[^+\d]/g, "");
    if (digits.length === 12 && digits.charAt(0) === "+") {
      return (
        digits.substr(0, 2) +
        " (" +
        digits.substr(2, 3) +
        ") " +
        digits.substr(5, 3) +
        "-" +
        digits.substr(8, 2) +
        "-" +
        digits.substr(10, 2)
      );
    }
    return phone;
  }

  // Fallback: подмена плейсхолдеров по тексту (для Tilda и других CMS без .kt-phone)
  function replacePlaceholdersByText(newPhone) {
    var formatted = formatPhone(newPhone);
    var telHref = "tel:" + newPhone.replace(/[^+\d]/g, "");

    // Заменяем href в <a tel:...> где номер состоит из нулей
    var links = document.querySelectorAll("a[href^=\"tel:\"]");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href") || "";
      // убираем нецифровые символы и проверяем что цифры — сплошные нули
      var hrefDigits = href.replace(/[^\d]/g, "");
      if (hrefDigits.length >= 7 && /^0+$/.test(hrefDigits)) {
        links[i].setAttribute("href", telHref);
      }
    }

    // Обходим текстовые ноды и заменяем плейсхолдеры
    if (!document.body) {
      return;
    }
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue) {
        PLACEHOLDER_RE.lastIndex = 0;
        if (PLACEHOLDER_RE.test(node.nodeValue)) {
          nodes.push(node);
        }
      }
    }
    for (var j = 0; j < nodes.length; j++) {
      PLACEHOLDER_RE.lastIndex = 0;
      nodes[j].nodeValue = nodes[j].nodeValue.replace(PLACEHOLDER_RE, formatted);
    }
    PLACEHOLDER_RE.lastIndex = 0;
  }

  // Применяет оба метода подмены: по CSS-классу и по тексту-плейсхолдеру
  function applyAll(phone) {
    replacePhones(phone);
    replacePlaceholdersByText(phone);
  }

  // --- API ---

  function apiRequest(endpoint, data, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open("POST", API_URL + endpoint, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("X-Api-Key", API_KEY);
    xhr.onreadystatechange = function () {
      if (xhr.readyState === 4) {
        if (xhr.status >= 200 && xhr.status < 300) {
          callback(null, JSON.parse(xhr.responseText));
        } else {
          callback(new Error("API error: " + xhr.status));
        }
      }
    };
    xhr.send(JSON.stringify(data));
  }

  // --- Инициализация ---

  function init() {
    var data = getTrafficSource();

    // Проверяем кеш — если номер уже получен для этого визита
    var cached = sessionStorage.getItem("kt_phone");
    if (cached) {
      applyAll(cached);
      // Повтор через 1.5с и 3.5с: Tilda дорисовывает блоки асинхронно
      setTimeout(function() { applyAll(cached); }, 1500);
      setTimeout(function() { applyAll(cached); }, 3500);
      startHeartbeat(data.client_id);
      return;
    }

    apiRequest("/tracking/get-number", data, function (err, res) {
      if (err) {
        console.error("[KuroTrack]", err);
        return;
      }

      sessionStorage.setItem("kt_phone", res.phone);
      sessionStorage.setItem("kt_session", res.session_id);
      applyAll(res.phone);
      // Повтор через 1.5с и 3.5с: Tilda дорисовывает блоки асинхронно
      setTimeout(function() { applyAll(res.phone); }, 1500);
      setTimeout(function() { applyAll(res.phone); }, 3500);
      startHeartbeat(res.session_id);
    });
  }

  function startHeartbeat(sessionId) {
    setInterval(function () {
      apiRequest("/tracking/heartbeat", { session_id: sessionId }, function () {});
    }, HEARTBEAT_INTERVAL);
  }

  // Запуск после загрузки DOM
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
