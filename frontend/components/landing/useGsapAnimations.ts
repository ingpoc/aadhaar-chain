import { useEffect } from 'react';
import gsap from 'gsap';
import ScrollTrigger from 'gsap/ScrollTrigger';

gsap.registerPlugin(ScrollTrigger);

// Progress bar animation
export const useProgressBar = () => {
  useEffect(() => {
    const progressBar = document.querySelector('.progress-bar');
    if (!progressBar) return;

    gsap.to(progressBar, {
      scaleX: 1,
      ease: 'none',
      scrollTrigger: {
        trigger: 'body',
        start: 'top top',
        end: 'bottom bottom',
        scrub: 0.3,
      },
    });
  }, []);
};

// Hero section animations
export const useHeroAnimations = () => {
  useEffect(() => {
    const heroTitle = document.querySelector('.hero-title');
    if (!heroTitle) return;

    const ctx = gsap.context(() => {
      const heroTl = gsap.timeline({ delay: 0.3 });

      // Fragmented identity paths fade in
      const fragmentedPaths = document.querySelectorAll('.fragmented-identity .fill-path');
      if (fragmentedPaths.length > 0) {
        heroTl.to(fragmentedPaths, {
          opacity: (i) => 0.1 + (i * 0.05),
          duration: 1.2,
          stagger: 0.15,
          ease: 'power2.out',
        });
      }

      // Unified identity core
      const unifiedCore = document.querySelector('.unified-identity .fill-path');
      if (unifiedCore) {
        heroTl.to(unifiedCore, {
          opacity: 0.2,
          scale: 1,
          duration: 0.8,
          ease: 'back.out(1.3)',
        }, 0.2);
      }

      // Radiating lines draw
      const drawPaths = document.querySelectorAll('.unified-identity .draw-path');
      if (drawPaths.length > 0) {
        // Initialize stroke dash
        drawPaths.forEach(path => {
          const length = (path as SVGPathElement).getTotalLength() || 200;
          (path as SVGPathElement).style.strokeDasharray = String(length);
          (path as SVGPathElement).style.strokeDashoffset = String(length);
        });

        heroTl.to(drawPaths, {
          strokeDashoffset: 0,
          opacity: 0.15,
          duration: 1.2,
          stagger: 0.1,
          ease: 'power2.inOut',
        }, 0.4);
      }

      // Text word reveal
      const words = document.querySelectorAll('.hero-title .word');
      if (words.length > 0) {
        heroTl.to(words, {
          y: 0,
          opacity: 1,
          duration: 1,
          stagger: 0.06,
          ease: 'power3.out',
        }, 0.3);
      }

      // Subtitle fade in
      const subtitle = document.querySelector('.hero-subtitle');
      if (subtitle) {
        heroTl.to(subtitle, { opacity: 1, duration: 1, ease: 'power2.out' }, 0.6);
      }

      // Scroll indicator
      const scrollIndicator = document.querySelector('.scroll-indicator');
      if (scrollIndicator) {
        heroTl.to(scrollIndicator, { opacity: 1, duration: 0.8 }, 0.8);
      }
    });

    return () => ctx.revert();
  }, []);
};

// Chapter animations - uses fill-path for shapes, draw-path for lines
export const useChapterAnimations = () => {
  useEffect(() => {
    const chapters = document.querySelectorAll('.chapter');
    if (chapters.length === 0) return;

    const ctx = gsap.context(() => {
      // Animate each chapter
      chapters.forEach((chapter) => {
        const illustration = chapter.querySelector('.illustration');
        if (!illustration) return;

        const content = chapter.querySelector('.chapter-content');
        const fillPaths = illustration.querySelectorAll('.fill-path');
        const drawPaths = illustration.querySelectorAll('.draw-path');
        const labels = illustration.querySelectorAll('.label-path');

        // Content reveal
        if (content) {
          gsap.to(content, {
            opacity: 1,
            y: 0,
            x: 0,
            duration: 1,
            ease: 'power3.out',
            scrollTrigger: {
              trigger: chapter,
              start: 'top 70%',
              toggleActions: 'play none none reverse',
            },
          });
        }

        // Fill paths (shapes) fade in with scale
        if (fillPaths.length > 0) {
          gsap.to(fillPaths, {
            opacity: 0.9,
            scale: 1,
            duration: 0.8,
            stagger: 0.1,
            ease: 'back.out(1.5)',
            scrollTrigger: {
              trigger: illustration,
              start: 'top 80%',
              toggleActions: 'play none none reverse',
            },
          });
        }

        // Draw paths (lines) animate stroke
        if (drawPaths.length > 0) {
          // Initialize stroke dash for draw effect
          drawPaths.forEach(path => {
            const length = (path as SVGPathElement).getTotalLength() || 1000;
            (path as SVGPathElement).style.strokeDasharray = String(length);
            (path as SVGPathElement).style.strokeDashoffset = String(length);
          });

          gsap.to(drawPaths, {
            strokeDashoffset: 0,
            opacity: 0.6,
            duration: 1.5,
            stagger: 0.1,
            ease: 'power2.inOut',
            scrollTrigger: {
              trigger: illustration,
              start: 'top 75%',
              toggleActions: 'play none none reverse',
            },
          });
        }

        // Labels fade in last
        if (labels.length > 0) {
          gsap.to(labels, {
            opacity: 0.7,
            duration: 0.5,
            stagger: 0.08,
            ease: 'power2.out',
            scrollTrigger: {
              trigger: illustration,
              start: 'top 70%',
              toggleActions: 'play none none reverse',
            },
          });
        }
      });

      // Chapter numbers parallax
      gsap.utils.toArray<HTMLElement>('.chapter-number').forEach((num) => {
        gsap.to(num, {
          y: 100,
          opacity: 0.02,
          ease: 'none',
          scrollTrigger: {
            trigger: num.parentElement as Element,
            start: 'top bottom',
            end: 'bottom top',
            scrub: 1,
          },
        });
      });
    });

    return () => ctx.revert();
  }, []);
};

// Finale animations
export const useFinaleAnimations = (shouldInitialize: boolean = true) => {
  useEffect(() => {
    if (!shouldInitialize) return;

    const finale = document.querySelector('.finale');
    const finaleIllustration = document.querySelector('.finale-illustration');
    if (!finale) return;

    const ctx = gsap.context(() => {
      // Container scale in
      if (finaleIllustration) {
        gsap.fromTo(finaleIllustration,
          { opacity: 0, scale: 0.9 },
          {
            opacity: 1,
            scale: 1,
            duration: 1.2,
            ease: 'power2.out',
            scrollTrigger: {
              trigger: finale,
              start: 'top 70%',
              toggleActions: 'play none none reverse',
            },
          }
        );
      }

      // Content fade in
      const finaleContent = finale.querySelector('.max-w-2xl');
      if (finaleContent) {
        gsap.fromTo(finaleContent,
          { opacity: 0, y: 30 },
          {
            opacity: 1,
            y: 0,
            duration: 1,
            ease: 'power2.out',
            scrollTrigger: {
              trigger: finale,
              start: 'top 60%',
              toggleActions: 'play none none reverse',
            },
          }
        );
      }

      // SVG fill paths
      const fillPaths = finale.querySelectorAll('.finale-illustration .fill-path');
      if (fillPaths.length > 0) {
        gsap.to(fillPaths, {
          opacity: 0.95,
          scale: 1,
          duration: 0.8,
          stagger: 0.1,
          ease: 'back.out(1.5)',
          scrollTrigger: {
            trigger: finaleIllustration,
            start: 'top 75%',
            toggleActions: 'play none none reverse',
          },
        });
      }

      // SVG draw paths
      const drawPaths = finale.querySelectorAll('.finale-illustration .draw-path');
      if (drawPaths.length > 0) {
        drawPaths.forEach(path => {
          const length = (path as SVGPathElement).getTotalLength() || 500;
          (path as SVGPathElement).style.strokeDasharray = String(length);
          (path as SVGPathElement).style.strokeDashoffset = String(length);
        });

        gsap.to(drawPaths, {
          strokeDashoffset: 0,
          opacity: 0.5,
          duration: 1.2,
          stagger: 0.08,
          ease: 'power2.inOut',
          scrollTrigger: {
            trigger: finaleIllustration,
            start: 'top 70%',
            toggleActions: 'play none none reverse',
          },
        });
      }
    });

    return () => ctx.revert();
  }, [shouldInitialize]);
};

// Custom cursor with smooth easing
export const useCustomCursor = () => {
  useEffect(() => {
    const cursorOuter = document.querySelector('.cursor-outer');
    const cursorInner = document.querySelector('.cursor-inner');
    const spotlightLayer = document.querySelector('.spotlight-layer');

    if (!cursorOuter || !cursorInner) return;

    let cursorX = 0, cursorY = 0;
    let dotX = 0, dotY = 0;
    let mouseX = 0, mouseY = 0;

    const handleMouseMove = (e: MouseEvent) => {
      mouseX = e.clientX;
      mouseY = e.clientY;

      if (spotlightLayer) {
        (spotlightLayer as HTMLElement).style.setProperty('--mouse-x', mouseX + 'px');
        (spotlightLayer as HTMLElement).style.setProperty('--mouse-y', mouseY + 'px');
      }
    };

    const updateCursor = () => {
      cursorX += (mouseX - cursorX) * 0.15;
      cursorY += (mouseY - cursorY) * 0.15;
      dotX += (mouseX - dotX) * 0.4;
      dotY += (mouseY - dotY) * 0.4;

      (cursorOuter as HTMLElement).style.left = cursorX - 20 + 'px';
      (cursorOuter as HTMLElement).style.top = cursorY - 20 + 'px';
      (cursorInner as HTMLElement).style.left = dotX - 4 + 'px';
      (cursorInner as HTMLElement).style.top = dotY - 4 + 'px';

      requestAnimationFrame(updateCursor);
    };

    document.addEventListener('mousemove', handleMouseMove);
    updateCursor();

    // Hide cursor on touch devices
    if ('ontouchstart' in window) {
      (cursorOuter as HTMLElement).style.display = 'none';
      (cursorInner as HTMLElement).style.display = 'none';
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
    };
  }, []);
};

// Spotlight layer observer for dark sections
export const useSpotlightObserver = () => {
  useEffect(() => {
    const spotlightLayer = document.querySelector('.spotlight-layer');
    if (!spotlightLayer) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const anyDarkVisible = entries.some(
          (e) => e.isIntersecting && e.intersectionRatio > 0.2
        );
        spotlightLayer.classList.toggle('active', anyDarkVisible);
      },
      { threshold: [0, 0.2, 0.4, 0.6, 0.8, 1] }
    );

    document.querySelectorAll('.landing-spotlight-target').forEach((section) => {
      observer.observe(section);
    });

    return () => observer.disconnect();
  }, []);
};

// Navbar hide on scroll
export const useNavbarHideOnScroll = () => {
  useEffect(() => {
    const navbar = document.querySelector('.landing-navbar');
    if (!navbar) return;

    let lastScrollY = 0;
    let tl: gsap.core.Tween | null = null;

    const handleScroll = () => {
      const currentScrollY = window.scrollY;
      const scrollDirection = currentScrollY > lastScrollY ? 'down' : 'up';

      if (scrollDirection === 'down' && currentScrollY > 100) {
        if (tl) tl.kill();
        tl = gsap.to('.landing-navbar', {
          y: -100,
          duration: 0.3,
          ease: 'power2.inOut',
        });
      } else {
        if (tl) tl.kill();
        tl = gsap.to('.landing-navbar', {
          y: 0,
          duration: 0.3,
          ease: 'power2.inOut',
        });
      }

      lastScrollY = currentScrollY;
    };

    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);
};
