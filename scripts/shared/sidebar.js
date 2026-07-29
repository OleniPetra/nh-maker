// Общий сайдбар Static Creo — единственный источник правды для навигации.
// Подключается ВСЕМИ страницами (review_ui.html, audiopng/static/index.html,
// competitors/static/index.html) по одному и тому же абсолютному пути
// /static-shared/sidebar.js. Каждая страница должна иметь только
// <div id="sc-sidebar-root"></div> — всё остальное (разметка, стили, клики,
// активная вкладка) собирает этот файл.
//
// Логотип (✺) сам по себе кликабелен и открывает AudioPng — добавить новый пункт
// SPA-навигации (или внешнее приложение со своим URL, через external) — правка
// только здесь, в массиве ITEMS.
(function () {
  var LOGO = {
    title: "AudioPng (генерация креативов + видео)",
    external: "/audiopng/",
  };

  var ITEMS = [
    {
      id: "creatives",
      title: "Creatives",
      icon: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="8" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/></svg>',
    },
    {
      id: "generate",
      title: "Generate",
      icon: '<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18"/></svg>',
    },
    {
      id: "history",
      title: "History",
      icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>',
    },
    {
      id: "competitors",
      title: "Competitors Static",
      external: "/competitors/",
      icon: '<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="4"/><circle cx="17" cy="16" r="4"/><path d="M8 12v0M13 13.5l1.5 1"/></svg>',
    },
  ];
  var SPA_IDS = ITEMS.filter(function (it) { return !it.external; }).map(function (it) { return it.id; });
  var EXTERNAL_ITEMS = ITEMS.filter(function (it) { return it.external; });

  var CSS = "" +
    "#sc-sidebar-root { display: flex; }" +
    ".sidebar { width: 76px; height: 100%; background: #221E17; display: flex; flex-direction: column;" +
    "  align-items: center; padding: 20px 0; gap: 24px; flex-shrink: 0; }" +
    ".sidebar .logo-btn {" +
    "  width: 44px; height: 44px; border-radius: 14px; border: none; background: transparent;" +
    "  color: #F0B429; font-size: 22px; font-weight: 700; display: flex; align-items: center;" +
    "  justify-content: center; cursor: pointer; }" +
    ".sidebar .logo-btn.active { background: #F0B429; color: #221E17; }" +
    ".sidebar nav { display: flex; flex-direction: column; gap: 10px; }" +
    ".sidebar button { width: 44px; height: 44px; border-radius: 14px; border: none; background: transparent;" +
    "  color: #A79E8E; display: flex; align-items: center; justify-content: center; cursor: pointer; }" +
    ".sidebar button svg { width: 20px; height: 20px; stroke: currentColor; fill: none; stroke-width: 1.8; }" +
    ".sidebar button.active { background: #F0B429; color: #221E17; }";

  function injectCss() {
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function onAudiopng() {
    return location.pathname.indexOf("/audiopng") === 0;
  }

  function onExternalItem(item) {
    return location.pathname.indexOf(item.external) === 0;
  }

  function onAnyExternal() {
    if (onAudiopng()) return true;
    return EXTERNAL_ITEMS.some(function (it) { return onExternalItem(it); });
  }

  function currentPageId() {
    if (onAnyExternal()) return null; // логотип/внешний пункт подсвечивается отдельно
    var h = location.hash.slice(1);
    return SPA_IDS.indexOf(h) !== -1 ? h : "creatives";
  }

  function goToAudiopng() {
    if (onAudiopng()) return; // уже там
    location.href = LOGO.external;
  }

  function navigate(item) {
    if (item.external) {
      if (onExternalItem(item)) return; // уже там
      location.href = item.external;
      return;
    }
    // Мы на корневом SPA (review_ui.html) — переключаем вкладку локально, без
    // перезагрузки. Если showPage почему-то не определён (например, скрипт этот
    // файл подключила чужая страница) — просто переходим на "/dashboard#id".
    if (typeof window.showPage === "function") {
      window.showPage(item.id);
    } else {
      location.href = "/dashboard#" + item.id;
    }
  }

  function build() {
    var root = document.getElementById("sc-sidebar-root");
    if (!root) return;

    var aside = document.createElement("aside");
    aside.className = "sidebar";
    aside.innerHTML = '<button class="logo-btn" id="sc-logo">✺</button><nav id="sc-nav"></nav>';
    root.appendChild(aside);

    var logoBtn = aside.querySelector("#sc-logo");
    logoBtn.title = LOGO.title;
    if (onAudiopng()) logoBtn.classList.add("active");
    logoBtn.onclick = goToAudiopng;

    var nav = aside.querySelector("#sc-nav");
    var active = currentPageId();
    ITEMS.forEach(function (item) {
      var b = document.createElement("button");
      b.innerHTML = item.icon;
      b.title = item.title;
      b.dataset.page = item.id;
      var isActive = item.external ? onExternalItem(item) : item.id === active;
      if (isActive) b.classList.add("active");
      b.onclick = function () { navigate(item); };
      nav.appendChild(b);
    });
  }

  injectCss();
  build();
})();
