// Общий сайдбар Static Creo — единственный источник правды для навигации.
// Подключается ВСЕМИ страницами (review_ui.html, audiopng/static/index.html,
// competitors/static/index.html, headline/static/index.html) по одному и тому же
// абсолютному пути /static-shared/sidebar.js. Каждая страница должна иметь только
// <div id="sc-sidebar-root"></div> — всё остальное (разметка, стили, клики,
// активная вкладка) собирает этот файл.
//
// Добавить новый пункт (отдельное приложение со своим URL через external, либо
// SPA-вкладку дашборда) — правка только здесь, в массивах TOP_ITEMS / DASH_ITEMS.
(function () {
  // Верхняя группа — отдельные приложения, каждое со своим URL. Прибита к верху экрана.
  var TOP_ITEMS = [
    {
      id: "audiopng",
      title: "AudioPng (генерация креативов + видео)",
      external: "/audiopng/",
      icon: '<svg viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="3"/><path d="M10 9.5l5 2.5-5 2.5z"/></svg>',
    },
    {
      id: "competitors",
      title: "Competitors Static",
      external: "/competitors/",
      icon: '<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="4"/><circle cx="17" cy="16" r="4"/><path d="M8 12v0M13 13.5l1.5 1"/></svg>',
    },
    {
      id: "headline",
      title: "Headline",
      external: "/headline/",
      icon: '<svg viewBox="0 0 24 24"><path d="M6 4v16M18 4v16M6 12h12"/></svg>',
    },
  ];

  // Нижняя группа — вкладки дашборда (/dashboard#id). Прибита к низу экрана и по
  // умолчанию скрыта: разворачивается стрелкой в самом низу бара.
  var DASH_ITEMS = [
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
  ];

  var DASH_IDS = DASH_ITEMS.map(function (it) { return it.id; });

  // Бар прибит к ЭКРАНУ, а не к странице: position:sticky + height:100vh. Раньше был
  // height:100% внутри flex-контейнера — на высокой скроллящейся странице (дашборд)
  // иконки уезжали вверх вместе с контентом и до нижней группы нужно было доскроллить.
  // sticky (а не fixed) — чтобы колонка в 76px по-прежнему занимала место в потоке и
  // не наезжала на контент справа.
  var CSS = "" +
    "#sc-sidebar-root { display: flex; position: sticky; top: 0; align-self: flex-start;" +
    "  height: 100vh; z-index: 30; }" +
    ".sidebar { width: 76px; height: 100%; background: #221E17; display: flex; flex-direction: column;" +
    "  align-items: center; padding: 14px 0; gap: 10px; flex-shrink: 0; }" +
    ".sidebar nav { display: flex; flex-direction: column; align-items: center; gap: 10px; }" +
    // Единый стиль для ВСЕХ кнопок бара (разделы, вкладки дашборда, стрелка) — одинаковый
    // размер, радиус и отступы, чтобы бар читался одним рядом равноудалённых иконок.
    ".sidebar button { width: 44px; height: 44px; border-radius: 14px; border: none; background: transparent;" +
    "  color: #A79E8E; display: flex; align-items: center; justify-content: center; cursor: pointer;" +
    "  flex-shrink: 0; padding: 0; transition: background .15s ease, color .15s ease; }" +
    ".sidebar button:hover { background: #2E2921; color: #EAE3D5; }" +
    ".sidebar button svg { width: 20px; height: 20px; stroke: currentColor; fill: none; stroke-width: 1.8;" +
    "  stroke-linecap: round; stroke-linejoin: round; }" +
    ".sidebar button.active, .sidebar button.active:hover { background: #F0B429; color: #221E17; }" +
    ".sidebar .sc-spacer { flex: 1 1 auto; }" +
    ".sidebar .sc-dash-nav { display: none; }" +
    ".sidebar .sc-dash-nav.open { display: flex; }" +
    ".sidebar .sc-dash-toggle svg { width: 16px; height: 16px; transition: transform .15s ease; }" +
    ".sidebar .sc-dash-toggle.open svg { transform: rotate(180deg); }";

  function injectCss() {
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function onExternalItem(item) {
    return location.pathname.indexOf(item.external) === 0;
  }

  function onAnyExternal() {
    return TOP_ITEMS.some(function (it) { return onExternalItem(it); });
  }

  function currentDashId() {
    if (onAnyExternal()) return null; // мы в отдельном приложении, вкладка дашборда не активна
    var h = location.hash.slice(1);
    return DASH_IDS.indexOf(h) !== -1 ? h : "creatives";
  }

  function navigate(item) {
    if (item.external) {
      if (onExternalItem(item)) return; // уже там
      location.href = item.external;
      return;
    }
    // Мы на корневом SPA (review_ui.html) — переключаем вкладку локально, без
    // перезагрузки. Если showPage почему-то не определён (например, этот файл
    // подключила чужая страница) — просто переходим на "/dashboard#id".
    if (typeof window.showPage === "function") {
      window.showPage(item.id);
    } else {
      location.href = "/dashboard#" + item.id;
    }
  }

  // Стрелка "вверх" — панель с вкладками дашборда разворачивается снизу вверх,
  // поэтому в закрытом состоянии стрелка смотрит вверх (раскрыть), а в открытом
  // разворачивается на 180° (свернуть). См. .sc-dash-toggle.open в CSS выше.
  var TOGGLE_ICON = '<svg viewBox="0 0 24 24"><path d="M18 15l-6-6-6 6"/></svg>';

  // Пары {item, button} — по ним syncActive() пересчитывает подсветку, не разбирая обратно
  // ни URL, ни data-атрибуты.
  var rendered = [];

  function makeButton(item) {
    var b = document.createElement("button");
    b.innerHTML = item.icon;
    b.title = item.title;
    b.dataset.page = item.id;
    b.onclick = function () { navigate(item); };
    rendered.push({ item: item, button: b });
    return b;
  }

  // Подсветку активного пункта бар считает САМ (и на старте, и по hashchange), а не ждёт,
  // что её проставит страница: раньше review_ui.html:showPage() лез в бар селектором
  // "#sc-nav button", и после разделения бара на две группы этот id перестал существовать —
  // вкладка переключалась, а подсветка молча застревала на предыдущей.
  function syncActive() {
    var activeDash = currentDashId();
    rendered.forEach(function (r) {
      var isActive = r.item.external ? onExternalItem(r.item) : r.item.id === activeDash;
      r.button.classList.toggle("active", isActive);
    });
  }

  function build() {
    var root = document.getElementById("sc-sidebar-root");
    if (!root) return;

    var aside = document.createElement("aside");
    aside.className = "sidebar";
    aside.innerHTML =
      '<nav id="sc-nav-top"></nav>' +
      '<div class="sc-spacer"></div>' +
      '<nav id="sc-nav-dash" class="sc-dash-nav"></nav>' +
      '<button class="sc-dash-toggle" id="sc-dash-toggle" title="Показать/скрыть разделы">' + TOGGLE_ICON + '</button>';
    root.appendChild(aside);

    var topNav = aside.querySelector("#sc-nav-top");
    TOP_ITEMS.forEach(function (item) { topNav.appendChild(makeButton(item)); });

    var dashNav = aside.querySelector("#sc-nav-dash");
    DASH_ITEMS.forEach(function (item) { dashNav.appendChild(makeButton(item)); });

    syncActive();
    // showPage() на дашборде переключает вкладку и пишет location.hash — ловим это здесь,
    // чтобы подсветка обновлялась независимо от того, кто именно сменил вкладку.
    window.addEventListener("hashchange", syncActive);

    // По умолчанию список вкладок дашборда скрыт (см. CSS .sc-dash-nav без .open).
    var toggleBtn = aside.querySelector("#sc-dash-toggle");
    toggleBtn.onclick = function () {
      dashNav.classList.toggle("open");
      toggleBtn.classList.toggle("open");
    };
  }

  injectCss();
  build();
})();
