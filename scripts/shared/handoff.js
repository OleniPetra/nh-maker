// Общий межстраничный "передаточный ящик" для файлов (картинок) между разделами Static Creo,
// которые живут как отдельные HTML-документы на одном origin (competitors/, audiopng/).
// Используется, например, кнопкой "Отправить в NH" в Competitors Static: она кладёт готовые
// картинки сюда и переходит на /audiopng/?import=1, а audiopng сам их оттуда забирает и
// прогоняет через свой addImages() — тот же путь, что и обычная загрузка в Upload 9:16.
//
// IndexedDB, а не localStorage/sessionStorage — там нельзя хранить Blob без ручной
// base64-сериализации, а тут файлы могут быть по несколько мегабайт каждая.
(function () {
  var DB_NAME = "nh-handoff";
  var STORE = "images";
  var KEY = "pending-upload-916";

  function openDb() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(STORE); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  // items: [{ name: string, blob: Blob }, ...]
  async function stage(items) {
    var db = await openDb();
    await new Promise(function (resolve, reject) {
      var tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(items, KEY);
      tx.oncomplete = resolve;
      tx.onerror = function () { reject(tx.error); };
    });
    db.close();
  }

  // Забирает и сразу удаляет staged-данные (одноразовый handoff, не подписка).
  async function consume() {
    var db = await openDb();
    var items = await new Promise(function (resolve, reject) {
      var tx = db.transaction(STORE, "readonly");
      var req = tx.objectStore(STORE).get(KEY);
      req.onsuccess = function () { resolve(req.result || null); };
      req.onerror = function () { reject(req.error); };
    });
    if (items) {
      await new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(KEY);
        tx.oncomplete = resolve;
        tx.onerror = function () { reject(tx.error); };
      });
    }
    db.close();
    return items;
  }

  window.NHHandoff = { stage: stage, consume: consume };
})();
