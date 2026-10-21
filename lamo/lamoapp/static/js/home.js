/* =========================================================
   LAMO MESSAGE TOAST CONTROLS
========================================================= */

function closeLamoToast(button) {
  const toast = button.closest(".lamo-toast");

  if (!toast) {
    return;
  }

  toast.classList.add("toast-closing");

  setTimeout(function () {
    toast.remove();
  }, 250);
}

document.addEventListener("DOMContentLoaded", function () {
  const toasts =
    document.querySelectorAll(".lamo-toast");

  toasts.forEach(function (toast) {

    setTimeout(function () {
      if (!toast.isConnected) {
        return;
      }

      toast.classList.add("toast-closing");

      setTimeout(function () {
        toast.remove();
      }, 250);

    }, 5000);

  });

  // Initialize 3D Stage & Coverflow Spotlight Carousel
  initServicesCoverflow();
});

/* =========================================================
   3D STAGE & COVERFLOW SPOTLIGHT CAROUSEL
========================================================= */
function initServicesCoverflow() {
  const container = document.getElementById("coverflowContainer");
  const track = document.getElementById("coverflowTrack");
  const stage = document.getElementById("servicesCoverflowStage");
  if (!container || !track || !stage) return;

  const slides = Array.from(track.querySelectorAll(".coverflow-slide"));
  const dots = Array.from(document.querySelectorAll(".coverflow-dot"));
  const prevBtn = document.getElementById("coverflowPrevBtn");
  const nextBtn = document.getElementById("coverflowNextBtn");

  const total = slides.length;
  if (total === 0) return;

  let currentIndex = 0;
  let autoPlayTimer = null;
  const AUTO_PLAY_INTERVAL = 3800;

  function getStepX() {
    const width = window.innerWidth;
    if (width < 576) return 130;
    if (width < 768) return 165;
    if (width < 992) return 215;
    return 270;
  }

  function updateCoverflow() {
    const stepX = getStepX();

    slides.forEach((slide, i) => {
      let diff = i - currentIndex;
      // Circular wrapping calculation
      while (diff > total / 2) diff -= total;
      while (diff < -total / 2) diff += total;

      if (diff === 0) {
        // Spotlight Center Card
        slide.style.transform = `translateX(0px) translateZ(95px) rotateY(0deg) scale(1.05)`;
        slide.style.zIndex = "10";
        slide.style.opacity = "1";
        slide.style.filter = "none";
        slide.style.pointerEvents = "auto";
        slide.classList.add("is-active");
      } else if (diff === 1) {
        // First Right Card
        slide.style.transform = `translateX(${stepX}px) translateZ(-25px) rotateY(-24deg) scale(0.88)`;
        slide.style.zIndex = "6";
        slide.style.opacity = "0.78";
        slide.style.filter = "blur(0.4px)";
        slide.style.pointerEvents = "auto";
        slide.classList.remove("is-active");
      } else if (diff === -1) {
        // First Left Card
        slide.style.transform = `translateX(-${stepX}px) translateZ(-25px) rotateY(24deg) scale(0.88)`;
        slide.style.zIndex = "6";
        slide.style.opacity = "0.78";
        slide.style.filter = "blur(0.4px)";
        slide.style.pointerEvents = "auto";
        slide.classList.remove("is-active");
      } else if (diff === 2) {
        // Second Right Card
        slide.style.transform = `translateX(${stepX * 1.78}px) translateZ(-100px) rotateY(-38deg) scale(0.72)`;
        slide.style.zIndex = "3";
        slide.style.opacity = "0.42";
        slide.style.filter = "blur(1px)";
        slide.style.pointerEvents = "auto";
        slide.classList.remove("is-active");
      } else if (diff === -2) {
        // Second Left Card
        slide.style.transform = `translateX(-${stepX * 1.78}px) translateZ(-100px) rotateY(38deg) scale(0.72)`;
        slide.style.zIndex = "3";
        slide.style.opacity = "0.42";
        slide.style.filter = "blur(1px)";
        slide.style.pointerEvents = "auto";
        slide.classList.remove("is-active");
      } else {
        // Far Cards (Hidden in depth)
        const sign = diff > 0 ? 1 : -1;
        slide.style.transform = `translateX(${sign * stepX * 2.3}px) translateZ(-190px) rotateY(${sign * -50}deg) scale(0.55)`;
        slide.style.zIndex = "1";
        slide.style.opacity = "0";
        slide.style.pointerEvents = "none";
        slide.classList.remove("is-active");
      }
    });

    // Update Dots
    dots.forEach((dot, idx) => {
      if (idx === currentIndex) {
        dot.classList.add("active");
        dot.setAttribute("aria-current", "true");
      } else {
        dot.classList.remove("active");
        dot.removeAttribute("aria-current");
      }
    });
  }

  function goToSlide(index) {
    currentIndex = (index + total) % total;
    updateCoverflow();
  }

  function nextSlide() {
    goToSlide(currentIndex + 1);
  }

  function prevSlide() {
    goToSlide(currentIndex - 1);
  }

  function startAutoPlay() {
    stopAutoPlay();
    if (total > 1) {
      autoPlayTimer = setInterval(nextSlide, AUTO_PLAY_INTERVAL);
    }
  }

  function stopAutoPlay() {
    if (autoPlayTimer) {
      clearInterval(autoPlayTimer);
      autoPlayTimer = null;
    }
  }

  // Event Listeners
  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      prevSlide();
      startAutoPlay();
    });
  }

  if (nextBtn) {
    nextBtn.addEventListener("click", () => {
      nextSlide();
      startAutoPlay();
    });
  }

  dots.forEach((dot, idx) => {
    dot.addEventListener("click", () => {
      goToSlide(idx);
      startAutoPlay();
    });
  });

  slides.forEach((slide, idx) => {
    slide.addEventListener("click", (e) => {
      // If clicking inside the active card's links, allow normal navigation
      if (slide.classList.contains("is-active")) {
        return;
      }
      e.preventDefault();
      goToSlide(idx);
      startAutoPlay();
    });
  });

  // Pause on hover
  stage.addEventListener("mouseenter", stopAutoPlay);
  stage.addEventListener("mouseleave", startAutoPlay);

  // Touch Swipe Support
  let touchStartX = 0;
  let touchEndX = 0;

  stage.addEventListener("touchstart", (e) => {
    touchStartX = e.changedTouches[0].screenX;
    stopAutoPlay();
  }, { passive: true });

  stage.addEventListener("touchend", (e) => {
    touchEndX = e.changedTouches[0].screenX;
    const swipeDist = touchEndX - touchStartX;
    if (Math.abs(swipeDist) > 40) {
      if (swipeDist > 0) {
        prevSlide();
      } else {
        nextSlide();
      }
    }
    startAutoPlay();
  }, { passive: true });

  // Page visibility change handling
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopAutoPlay();
    } else {
      startAutoPlay();
    }
  });

  // Handle window resizing
  let resizeTimeout;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(updateCoverflow, 120);
  });

  // Initial render & auto-play kick-off
  updateCoverflow();
  startAutoPlay();
}