/* ========================================
   SEO記事共通JS
   WordPressの「カスタムCSS & JS」に貼り付けて使用
   全ジャンル（AGA/ED/脱毛等）で共通
   ======================================== */

/* --- strong 蛍光マーカー スクロールアニメーション --- */
document.addEventListener("DOMContentLoaded", function () {
  var targets = document.querySelectorAll("p strong");
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
