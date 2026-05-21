/* ========================================
   SEO記事共通JS
   WordPressの「カスタムCSS & JS」に貼り付けて使用
   全ジャンル（AGA/ED/脱毛等）で共通
   ======================================== */

/* --- strong 蛍光マーカー スクロールアニメーション --- */
document.addEventListener("DOMContentLoaded", function () {
  var targets = document.querySelectorAll("p strong, li strong");
  if (!targets.length) return;

  var observer = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.5 }
  );

  targets.forEach(function (el) {
    observer.observe(el);
  });
});

/* --- SWELL 目次トグル（初期は閉じる） --- */
document.addEventListener("DOMContentLoaded", function () {
  var tocs = document.querySelectorAll(".p-toc");
  if (!tocs.length) return;

  tocs.forEach(function (toc) {
    var title = toc.querySelector(".p-toc__ttl");
    if (!title) return;

    toc.classList.remove("is-open");
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");

    var label = title.querySelector(".seo-toc-toggle-label");
    if (!label) {
      label = document.createElement("span");
      label.className = "seo-toc-toggle-label";
      title.appendChild(label);
    }

    function syncState() {
      var isOpen = toc.classList.contains("is-open");
      title.setAttribute("aria-expanded", isOpen ? "true" : "false");
      label.textContent = isOpen ? "目次を閉じる" : "目次を開く";
    }

    syncState();

    title.addEventListener("click", function () {
      // テーマ側のトグル後に状態同期する
      setTimeout(syncState, 0);
    });

    title.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      toc.classList.toggle("is-open");
      syncState();
    });
  });
});
