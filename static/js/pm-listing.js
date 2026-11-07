/* Perk Miner listing photo slider — works on the live listing and on dynamically inserted previews. */
(function () {
  function slides(s) { return s.querySelectorAll('.pml-slide'); }
  function index(s) {
    var track = s.querySelector('.pml-slides'), w = track.clientWidth || 1;
    return Math.max(0, Math.min(slides(s).length - 1, Math.round(track.scrollLeft / w)));
  }
  function sync(s) {
    var i = index(s), n = slides(s).length;
    var dots = s.querySelectorAll('.pml-dots button');
    for (var d = 0; d < dots.length; d++) dots[d].classList.toggle('on', d === i);
    var c = s.querySelector('.pml-count'); if (c) c.textContent = (i + 1) + ' / ' + n;
    var th = s.parentNode.querySelectorAll('.pml-thumbs button');
    for (var t = 0; t < th.length; t++) th[t].classList.toggle('on', t === i);
  }
  function go(s, i) {
    var n = slides(s).length, track = s.querySelector('.pml-slides');
    i = (i + n) % n;
    track.scrollTo({ left: i * track.clientWidth, behavior: 'smooth' });
  }
  function root(el) { return el.closest ? el.closest('.pml-slider') : null; }
  document.addEventListener('click', function (e) {
    var nav = e.target.closest && e.target.closest('.pml-nav');
    if (nav) { var s = root(nav); if (s) { e.preventDefault(); go(s, index(s) + (nav.classList.contains('next') ? 1 : -1)); } return; }
    var dot = e.target.closest && e.target.closest('.pml-dots button, .pml-thumbs button');
    if (dot) {
      var s2 = dot.closest('.pml-slider') || dot.closest('.pml-gallery-wrap').querySelector('.pml-slider');
      var k = Array.prototype.indexOf.call(dot.parentNode.children, dot);
      if (s2) { e.preventDefault(); go(s2, k); }
    }
  });
  document.addEventListener('scroll', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('pml-slides')) { var s = root(e.target); if (s) sync(s); }
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    var s = e.target.closest && e.target.closest('.pml-slider'); if (!s) return;
    e.preventDefault(); go(s, index(s) + (e.key === 'ArrowRight' ? 1 : -1));
  });
  // gentle autoplay while visible and not hovered / focused
  setInterval(function () {
    var all = document.querySelectorAll('.pml-slider[data-autoplay]');
    for (var i = 0; i < all.length; i++) {
      var s = all[i], r = s.getBoundingClientRect();
      if (slides(s).length < 2 || s.matches(':hover') || s.contains(document.activeElement)) continue;
      if (r.bottom > 0 && r.top < window.innerHeight) go(s, index(s) + 1);
    }
  }, 5000);
  window.addEventListener('resize', function () { document.querySelectorAll('.pml-slider').forEach(function (s) { var t = s.querySelector('.pml-slides'); t.scrollLeft = index(s) * t.clientWidth; }); });
})();
