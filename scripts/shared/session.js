// Сохранение состояния страниц Static Creo между перезагрузками, переходами и
// переключением вкладок.
//
// IndexedDB, а не localStorage: в состоянии лежат File/Blob (загруженные и сгенерированные
// картинки, аудио) — это десятки мегабайт, которые в localStorage не влезут в принципе, да и
// хранить он умеет только строки. IndexedDB же кладёт File/Blob как есть, через structured
// clone, без ручной base64-сериализации.
//
// Каждая страница регистрирует себя через NHSession.attach() и отдаёт две функции: collect()
// собирает состояние в простой объект, restore() применяет его обратно. Всё остальное —
// дебаунс, запись при уходе со страницы, обработка переполнения квоты, кнопка сброса —
// одинаковое для всех и живёт здесь.
(function () {
  var DB_NAME = "nh-session";
  var STORE = "state";

  function openDb() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () {
        if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  async function save(pageId, state) {
    var db = await openDb();
    try {
      await new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).put(state, pageId);
        tx.oncomplete = resolve;
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error); };
      });
    } finally {
      db.close();
    }
  }

  async function load(pageId) {
    var db = await openDb();
    try {
      return await new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, "readonly");
        var req = tx.objectStore(STORE).get(pageId);
        req.onsuccess = function () { resolve(req.result || null); };
        req.onerror = function () { reject(req.error); };
      });
    } finally {
      db.close();
    }
  }

  async function clear(pageId) {
    var db = await openDb();
    try {
      await new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(pageId);
        tx.oncomplete = resolve;
        tx.onerror = function () { reject(tx.error); };
      });
    } finally {
      db.close();
    }
  }

  // Подключает страницу к автосохранению.
  //   pageId  — ключ в хранилище ("audiopng" / "competitors" / "headline")
  //   collect — () => объект состояния (только structured-clone-совместимое)
  //   restore — async (state) => применить состояние; вызывается один раз при старте
  // Возвращает { ready, flush, reset }.
  function attach(pageId, collect, restore, opts) {
    opts = opts || {};
    var debounceMs = opts.debounceMs || 600;
    var timer = null;
    var suspended = true;   // не сохраняем, пока идёт восстановление
    var lastError = null;

    async function write() {
      if (suspended) return;
      try {
        await save(pageId, collect());
        lastError = null;
      } catch (e) {
        // Чаще всего — QuotaExceededError: место кончилось. Роняем автосохранение молча
        // (в консоль), но НЕ ломаем саму страницу: работать с приложением можно и без кэша.
        if (lastError !== String(e)) {
          lastError = String(e);
          console.warn("[session] не удалось сохранить состояние:", e);
        }
      }
    }

    function schedule() {
      if (suspended) return;
      clearTimeout(timer);
      timer = setTimeout(write, debounceMs);
    }

    function flush() {
      clearTimeout(timer);
      return write();
    }

    // Вкладку скрыли/уходим со страницы — дописываем не дожидаясь дебаунса. visibilitychange
    // надёжнее beforeunload: он срабатывает и при переключении вкладок, и при сворачивании,
    // и на мобильных, где beforeunload часто не приходит вовсе.
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flush();
    });
    window.addEventListener("pagehide", flush);

    var ready = (async function () {
      try {
        var state = await load(pageId);
        if (state) await restore(state);
      } catch (e) {
        console.warn("[session] не удалось восстановить состояние:", e);
      } finally {
        suspended = false;   // с этого момента любые изменения пишутся
      }
    })();

    async function reset() {
      suspended = true;
      clearTimeout(timer);
      try { await clear(pageId); } catch (e) { console.warn("[session] сброс:", e); }
      location.reload();
    }

    return { ready: ready, flush: flush, schedule: schedule, reset: reset };
  }

  // Кнопка «Сбросить» в шапке. Спрашивает подтверждение — состояние может содержать
  // результаты платных генераций, терять их по случайному клику нельзя.
  function resetButton(session, label) {
    var b = document.createElement("button");
    b.className = "btn";
    b.type = "button";
    b.title = "Очистить сохранённое состояние этой страницы";
    b.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 2v6h6"/><path d="M3 13a9 9 0 1 0 3-7.7L3 8"/></svg>' +
      '<span>' + (label || "Сбросить") + '</span>';
    b.addEventListener("click", function () {
      if (confirm("Очистить всё на этой странице? Загруженные и сгенерированные файлы будут потеряны.")) {
        session.reset();
      }
    });
    return b;
  }

  window.NHSession = { save: save, load: load, clear: clear, attach: attach, resetButton: resetButton };
})();
