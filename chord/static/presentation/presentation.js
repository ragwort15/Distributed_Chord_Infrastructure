/* =========================================================
   CHORD DHT — Presentation Animations
   Lenis smooth scroll + GSAP ScrollTrigger
   Inspired by realfood.gov scroll-scrub patterns
   ========================================================= */

gsap.registerPlugin(ScrollTrigger);

/* ─────────────────────────────────────────────────────────
   LENIS — Physics-based smooth scroll (same as realfood.gov)
───────────────────────────────────────────────────────── */
const lenis = new Lenis({
  lerp: 0.08,
  smooth: true,
  direction: 'vertical',
  gestureDirection: 'vertical',
  smoothTouch: false,
});

lenis.on('scroll', ScrollTrigger.update);

gsap.ticker.add((time) => {
  lenis.raf(time * 1000);
});
gsap.ticker.lagSmoothing(0);

/* ─────────────────────────────────────────────────────────
   SCROLL PROGRESS BAR
───────────────────────────────────────────────────────── */
const progressBar = document.getElementById('scroll-progress');
lenis.on('scroll', ({ progress }) => {
  progressBar.style.width = `${progress * 100}%`;
});

/* ─────────────────────────────────────────────────────────
   PAGE INDICATOR — dots nav (realfood.gov style)
───────────────────────────────────────────────────────── */
const indicator    = document.getElementById('page-indicator');
const indLabel     = indicator.querySelector('.ind-label');
const indDots      = [...indicator.querySelectorAll('.ind-dot')];
const indBack      = indicator.querySelector('.ind-back');
const dotSections  = ['problem','solution','architecture','ai-agents','resilience','demo-cta','impact'];

// Click back → scroll to top
indBack.addEventListener('click', () => lenis.scrollTo('#hero', { duration: 1.4 }));

// Click dots → scroll to section
indDots.forEach((dot, i) => {
  dot.addEventListener('click', () => {
    lenis.scrollTo(`#${dotSections[i]}`, { duration: 1.2, offset: -60 });
  });
});

function setActiveSection(sectionId) {
  const idx = dotSections.indexOf(sectionId);
  if (idx === -1) return;

  // Find section's label from data attribute
  const el = document.querySelector(`#${sectionId}`);
  const label = el ? el.dataset.indLabel : sectionId;

  indLabel.textContent = label;

  // Update dots: active dot gets accent class, others muted
  indDots.forEach((dot, i) => {
    dot.classList.toggle('active', i === idx);
    dot.style.opacity = i === idx ? '0' : '1'; // hide the dot that's now the label
  });

  // Show indicator
  indicator.classList.add('visible');
}

// IntersectionObserver to track which dot section is active
const sectionObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      setActiveSection(entry.target.id);
    }
  });
}, { threshold: 0.3 });

dotSections.forEach(id => {
  const el = document.getElementById(id);
  if (el) sectionObserver.observe(el);
});

// Hide indicator on hero/video sections
const heroObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) indicator.classList.remove('visible');
  });
}, { threshold: 0.5 });

['hero','video-section'].forEach(id => {
  const el = document.getElementById(id);
  if (el) heroObserver.observe(el);
});

/* ─────────────────────────────────────────────────────────
   HERO — Initial animations (on load)
───────────────────────────────────────────────────────── */
const heroTl = gsap.timeline({ defaults: { ease: 'power3.out' } });

heroTl
  .from('.hero-eyebrow', { opacity: 0, y: -20, duration: 0.8 })
  .from('#hero-title', { opacity: 0, y: 60, duration: 1, ease: 'power4.out' }, '-=0.4')
  .from('#hero-subtitle', { opacity: 0, y: 30, duration: 0.8 }, '-=0.5')
  .from('#hero-video-wrapper', { opacity: 0, y: 60, duration: 1 }, '-=0.3');

/* ─────────────────────────────────────────────────────────
   HERO VIDEO EXPAND — scroll-scrub (core realfood.gov effect)
   As user scrolls, the video strip grows to fill viewport,
   hero text fades up and out, border-radius collapses to 0.
───────────────────────────────────────────────────────── */
const heroVideoTl = gsap.timeline({
  scrollTrigger: {
    trigger: '#hero',
    start: 'top top',
    end: '+=120%',
    pin: true,
    scrub: 0.8,
    anticipatePin: 1,
  }
});

heroVideoTl
  .to('.hero-eyebrow', { opacity: 0, y: -40, duration: 0.3 }, 0)
  .to('#hero-title',   { opacity: 0, y: -60, duration: 0.4 }, 0.05)
  .to('#hero-subtitle',{ opacity: 0, y: -30, duration: 0.3 }, 0.05)
  .to('#hero-video-wrapper', {
    width: '100%',
    height: '100vh',
    borderRadius: '0px',
    duration: 1,
    ease: 'none',
  }, 0.1);

/* ─────────────────────────────────────────────────────────
   VIDEO SECTION — reveal overlay button, then fade out
───────────────────────────────────────────────────────── */
gsap.timeline({
  scrollTrigger: {
    trigger: '#video-section',
    start: 'top 80%',
    end: 'top 20%',
    scrub: 1,
  }
})
.to('#video-overlay-btn', {
  opacity: 1,
  pointerEvents: 'auto',
  duration: 0.5,
})
.to('#video-full-container', {
  width: '94%',
  borderRadius: '4px',
  duration: 0.5,
}, 0);

// Fade video section out as problem section approaches
gsap.to('#video-section', {
  scrollTrigger: {
    trigger: '#problem',
    start: 'top 80%',
    end: 'top top',
    scrub: 1,
  },
  opacity: 0,
});

/* ─────────────────────────────────────────────────────────
   PROBLEM — Stat boxes grow from small (realfood.gov body.png)
   Boxes start small/flat and grow upward with stagger
───────────────────────────────────────────────────────── */
gsap.from('.problem-headline', {
  scrollTrigger: { trigger: '#problem', start: 'top 70%' },
  opacity: 0, y: 60, duration: 0.9, ease: 'power3.out',
});

// Stat boxes scale-up from 0 with stagger (like the 50/75/90% boxes)
gsap.from(['#stat1', '#stat2', '#stat3'], {
  scrollTrigger: {
    trigger: '.stat-boxes',
    start: 'top 75%',
    end: 'bottom 30%',
    scrub: 0.6,
  },
  scaleY: 0,
  transformOrigin: 'bottom center',
  stagger: 0.15,
  opacity: 0,
  ease: 'power2.out',
});

// Numbers count up
function animateCounter(el, target, suffix = '') {
  const obj = { val: 0 };
  gsap.to(obj, {
    scrollTrigger: { trigger: el, start: 'top 80%' },
    val: target,
    duration: 1.8,
    ease: 'power2.out',
    onUpdate() {
      el.querySelector('.stat-box-num').textContent =
        (suffix === '$' ? '$' : '') +
        Math.round(obj.val).toLocaleString() +
        (suffix === '$' ? 'K' : suffix);
    }
  });
}

/* ─────────────────────────────────────────────────────────
   SOLUTION — Word-by-word scroll-scrub reveal
   Like transition,animation.mov — words light up as you scroll,
   dim if you scroll back. Fully bidirectional scrub.
───────────────────────────────────────────────────────── */
const words = document.querySelectorAll('.reveal-text .word');

gsap.to(words, {
  scrollTrigger: {
    trigger: '#solution',
    start: 'top 70%',
    end: 'bottom 35%',
    scrub: 0.6,
  },
  opacity: 1,
  y: 0,
  stagger: { each: 0.04, from: 'start' },
  ease: 'none',
});

// Also fade section label + caption
gsap.from('#solution .section-label', {
  scrollTrigger: { trigger: '#solution', start: 'top 70%' },
  opacity: 0, y: 20, duration: 0.7,
});
gsap.from('.solution-caption', {
  scrollTrigger: { trigger: '#solution', start: 'bottom 70%' },
  opacity: 0, y: 20, duration: 0.7,
});

/* ─────────────────────────────────────────────────────────
   ARCHITECTURE — SVG elements animate in with scroll-scrub
   Each element draws/fades in sequentially as pinned section scrolls
───────────────────────────────────────────────────────── */
const archTl = gsap.timeline({
  scrollTrigger: {
    trigger: '#architecture',
    start: 'top 60%',
    end: 'bottom 20%',
    scrub: 0.5,
    toggleActions: 'play none none reverse',
  }
});

archTl
  // 1. Headline / subtitle
  .from('.arch-headline', { opacity: 0, y: 40, duration: 0.4 }, 0)
  .from('.arch-subtitle', { opacity: 0, y: 20, duration: 0.3 }, 0.1)
  // 2. Ring circle draws in
  .fromTo('#ring-circle',
    { strokeDasharray: 1200, strokeDashoffset: 1200 },
    { strokeDashoffset: 0, duration: 0.6, ease: 'none' }, 0.2)
  // 3. Client box
  .to('#g-client', { opacity: 1, duration: 0.3 }, 0.6)
  // 4. Arrow client → A
  .to('#arr-client-a', { strokeDashoffset: 0, opacity: 0.8, duration: 0.3 }, 0.8)
  // 5. Node A (entry)
  .to('#g-node-a', { opacity: 1, scale: 1, duration: 0.4, ease: 'back.out(1.5)', transformOrigin: 'center' }, 0.9)
  .to('#lbl-entry', { opacity: 1, duration: 0.2 }, 1.1)
  // 6. Route to B
  .to('#g-node-b', { opacity: 1, duration: 0.3 }, 1.2)
  .to('#arr-ab', { strokeDashoffset: 0, opacity: 0.9, duration: 0.4, ease: 'none' }, 1.3)
  .to('#lbl-route', { opacity: 1, duration: 0.2 }, 1.5)
  // 7. Route to C (the owner)
  .to('#arr-bc', { strokeDashoffset: 0, opacity: 0.9, duration: 0.4, ease: 'none' }, 1.6)
  .to('#g-node-c', { opacity: 1, duration: 0.4, ease: 'back.out(1.5)', transformOrigin: 'center' }, 1.8)
  .to('#lbl-owner', { opacity: 1, duration: 0.2 }, 2.0)
  // 8. Replication arrows to D & E
  .to('#arr-cd', { strokeDashoffset: 0, opacity: 0.8, duration: 0.4 }, 2.1)
  .to('#g-node-d', { opacity: 1, duration: 0.3 }, 2.2)
  .to('#lbl-replica', { opacity: 1, duration: 0.2 }, 2.3)
  .to('#arr-ce', { strokeDashoffset: 0, opacity: 0.8, duration: 0.4 }, 2.3)
  .to('#g-node-e', { opacity: 1, duration: 0.3 }, 2.4)
  .to('#lbl-replica2', { opacity: 1, duration: 0.2 }, 2.5)
  // 9. AI box + dashed lines
  .to('#g-ai', { opacity: 1, duration: 0.4 }, 2.6)
  .to(['#ai-line-a','#ai-line-b','#ai-line-c'], { opacity: 1, duration: 0.4, stagger: 0.1 }, 2.7)
  // 10. Observability box + lines
  .to('#g-obs', { opacity: 1, duration: 0.4 }, 3.0)
  .to(['#obs-line-a','#obs-line-c','#obs-line-d'], { opacity: 1, duration: 0.4, stagger: 0.1 }, 3.1)
  // 11. Legend
  .to('#arch-legend', { opacity: 1, duration: 0.4 }, 3.3);

/* ─────────────────────────────────────────────────────────
   AI AGENTS — Card movement (card movement.mov style)
   Cards start stacked (offset Y, different X) then fan out.
   As the section enters view, cards fly to their grid positions.
───────────────────────────────────────────────────────── */
// Initial state: cards stacked behind each other, slightly rotated
gsap.set('#agent-card-1', { y: 60, x: -40, rotation: -4, opacity: 0 });
gsap.set('#agent-card-2', { y: 80, x: 0,   rotation: 0,  opacity: 0 });
gsap.set('#agent-card-3', { y: 60, x: 40,  rotation: 4,  opacity: 0 });

// Fan out as section enters
gsap.to(['#agent-card-1','#agent-card-2','#agent-card-3'], {
  scrollTrigger: {
    trigger: '#ai-agents',
    start: 'top 65%',
    end: 'top 20%',
    scrub: 0.7,
  },
  y: 0,
  x: 0,
  rotation: 0,
  opacity: 1,
  stagger: { each: 0.12, from: 'start' },
  ease: 'power2.out',
});

// Headline + subtitle stagger in
gsap.from('.agents-headline', {
  scrollTrigger: { trigger: '#ai-agents', start: 'top 70%' },
  opacity: 0, y: 50, duration: 0.9, ease: 'power3.out',
});
gsap.from('.agents-subtitle', {
  scrollTrigger: { trigger: '#ai-agents', start: 'top 65%' },
  opacity: 0, y: 30, duration: 0.7, delay: 0.15, ease: 'power2.out',
});

// Decision log fades in after cards
gsap.to('#decision-log', {
  scrollTrigger: { trigger: '#decision-log', start: 'top 80%' },
  opacity: 1, y: 0, duration: 0.8,
});
gsap.set('#decision-log', { y: 30 });

/* ─────────────────────────────────────────────────────────
   RESILIENCE — Timeline items slide in with stagger
───────────────────────────────────────────────────────── */
gsap.from('.res-headline', {
  scrollTrigger: { trigger: '#resilience', start: 'top 70%' },
  opacity: 0, y: 50, duration: 0.9, ease: 'power3.out',
});

gsap.to(['#tl-1','#tl-2','#tl-3','#tl-4','#tl-5'], {
  scrollTrigger: {
    trigger: '#failure-timeline',
    start: 'top 70%',
    end: 'bottom 30%',
    scrub: 0.5,
  },
  opacity: 1,
  x: 0,
  stagger: { each: 0.2, from: 'start' },
  ease: 'power2.out',
});

/* ─────────────────────────────────────────────────────────
   DEMO CTA — Dramatic stagger reveal
───────────────────────────────────────────────────────── */
const demoTl = gsap.timeline({
  scrollTrigger: { trigger: '#demo-cta', start: 'top 60%' }
});

demoTl
  .to('#demo-eyebrow',   { opacity: 1, y: 0, duration: 0.6, ease: 'power2.out' })
  .to('#demo-headline',  { opacity: 1, y: 0, duration: 0.8, ease: 'power3.out' }, '-=0.2')
  .to('#demo-previews',  { opacity: 1, y: 0, duration: 0.6, ease: 'power2.out' }, '-=0.3')
  .to('#demo-btn-wrap',  { opacity: 1, y: 0, duration: 0.6, ease: 'power2.out' }, '-=0.2');

gsap.set(['#demo-headline','#demo-previews','#demo-btn-wrap'], { y: 40 });

/* ─────────────────────────────────────────────────────────
   IMPACT — Cards stagger in (card movement.mov style)
───────────────────────────────────────────────────────── */
gsap.from('.impact-headline', {
  scrollTrigger: { trigger: '#impact', start: 'top 70%' },
  opacity: 0, y: 50, duration: 0.9, ease: 'power3.out',
});
gsap.from('.impact-sub', {
  scrollTrigger: { trigger: '#impact', start: 'top 65%' },
  opacity: 0, y: 30, duration: 0.7, delay: 0.1,
});

gsap.set(['#ic1','#ic2','#ic3'], { y: 60, opacity: 0, rotation: (i) => [2, 0, -2][i] });
gsap.to(['#ic1','#ic2','#ic3'], {
  scrollTrigger: {
    trigger: '.impact-grid',
    start: 'top 75%',
    end: 'top 30%',
    scrub: 0.6,
  },
  y: 0,
  opacity: 1,
  rotation: 0,
  stagger: { each: 0.15, from: 'start' },
  ease: 'power2.out',
});

gsap.from('.achievement', {
  scrollTrigger: { trigger: '.impact-achievements', start: 'top 80%' },
  opacity: 0, x: -20, duration: 0.5,
  stagger: { each: 0.08, from: 'start' },
  ease: 'power2.out',
});

/* ─────────────────────────────────────────────────────────
   Q&A — Fade in
───────────────────────────────────────────────────────── */
gsap.from('.qna-headline', {
  scrollTrigger: { trigger: '#qna', start: 'top 70%' },
  opacity: 0, y: 60, duration: 1.0, ease: 'power3.out',
});
gsap.from(['.qna-team','.qna-links'], {
  scrollTrigger: { trigger: '#qna', start: 'top 65%' },
  opacity: 0, y: 30, duration: 0.7, stagger: 0.15, ease: 'power2.out',
});

/* ─────────────────────────────────────────────────────────
   WORD INITIAL STATE — words start dim (for reveal effect)
───────────────────────────────────────────────────────── */
// Wrap plain text nodes in .reveal-text into word spans
(function splitRevealText() {
  const rt = document.getElementById('reveal-text');
  if (!rt) return;
  // Walk through already-marked .word spans (set in HTML)
  // Just ensure initial opacity is 0.15 for the scroll reveal
  const wordSpans = rt.querySelectorAll('.word');
  wordSpans.forEach(w => {
    gsap.set(w, { opacity: 0.12, y: 8 });
  });
})();

/* ─────────────────────────────────────────────────────────
   MISC — Section label reveals
───────────────────────────────────────────────────────── */
document.querySelectorAll('.section-label').forEach(el => {
  gsap.from(el, {
    scrollTrigger: { trigger: el, start: 'top 85%' },
    opacity: 0, y: 15, duration: 0.5, ease: 'power2.out',
  });
});

/* ─────────────────────────────────────────────────────────
   RING CIRCLE — initial state for dash animation
───────────────────────────────────────────────────────── */
gsap.set('#ring-circle', { strokeDasharray: 1200, strokeDashoffset: 1200 });

/* ─────────────────────────────────────────────────────────
   REFRESH ScrollTrigger after fonts/images load
───────────────────────────────────────────────────────── */
window.addEventListener('load', () => {
  ScrollTrigger.refresh();
});
