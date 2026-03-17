'use client';

import { useEffect } from 'react';
import Link from 'next/link';
import type { ReactNode } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';
import gsap from 'gsap';
import ScrollTrigger from 'gsap/ScrollTrigger';
import {
  useProgressBar,
  useHeroAnimations,
  useChapterAnimations,
  useFinaleAnimations,
  useCustomCursor,
  useSpotlightObserver,
  useNavbarHideOnScroll,
} from './useGsapAnimations';
import { Navbar } from './Navbar';
import { Button } from '@/components/ui/button';

gsap.registerPlugin(ScrollTrigger);

const HeroSection = () => (
  <section className="hero landing-hero">
    <svg
      viewBox="0 0 1000 1000"
      fill="none"
      className="absolute top-0 left-0 w-full h-full opacity-[0.12] pointer-events-none"
    >
      <g className="fragmented-identity">
        <path
          className="fill-path"
          d="M 150 300 Q 130 320 140 360 L 160 400 Q 170 420 195 415 L 210 375 Q 215 335 195 315 Z"
          fill="#141413"
          opacity="0"
        />
        <path
          className="fill-path"
          d="M 280 250 Q 265 270 275 310 L 295 350 Q 305 370 330 365 L 345 325 Q 350 285 330 265 Z"
          fill="#141413"
          opacity="0"
        />
      </g>

      <g className="unified-identity">
        <path
          className="fill-path"
          d="M 700 400 Q 660 430 675 490 Q 695 550 770 560 Q 845 550 865 490 Q 880 430 840 400 Z"
          fill="#141413"
          opacity="0"
        />
        <path
          className="draw-path"
          d="M 770 320 Q 800 380 770 440"
          fill="none"
          stroke="#141413"
          strokeWidth="2"
          opacity="0"
        />
      </g>
    </svg>

    <div className="landing-hero-copy">
      <h1 className="hero-title landing-h1 text-center">
        <span className="block">
          <span className="landing-word word">Imagine</span>
        </span>
        <span className="block">
          <span className="landing-word word">a world</span>
        </span>
        <span className="block">
          <span className="landing-word word">where</span>
        </span>
        <span className="block">
          <span className="landing-word word">identity</span>
        </span>
        <span className="block">
          <span className="landing-word word">travels</span>
        </span>
        <span className="block">
          <span className="landing-word word">freely.</span>
        </span>
      </h1>

      <p className="hero-subtitle landing-subtitle opacity-0">
        Government-grade verification meets self-sovereign ownership.
        <br />
        Unlock human potential across borders without losing control.
      </p>
    </div>

    <div className="scroll-indicator absolute bottom-8 opacity-0 flex flex-col items-center gap-2 text-xs uppercase tracking-widest text-[var(--charcoal)]">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <path
          d="M12 16V8M12 16L16 12M12 16L8 12"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>Scroll</span>
    </div>
  </section>
);

interface ChapterSectionProps {
  number: number;
  label: string;
  description: {
    title: string;
    paragraphs: string[];
  };
  isLight: boolean;
  children: ReactNode;
}

const ChapterSection = ({ number, label, description, isLight, children }: ChapterSectionProps) => (
  <section
    className={`chapter landing-section ${isLight ? 'landing-section-light light' : 'landing-section-dark landing-spotlight-target'}`}
  >
    <span className="chapter-number landing-chapter-number">
      {String(number).padStart(2, '0')}
    </span>

    <div className="chapter-inner landing-chapter-grid relative z-10">
      {number % 2 === 0 ? (
        <>
          {children}
          <div className="chapter-content landing-chapter-content">
            <p className="chapter-label text-xs md:text-sm font-semibold uppercase tracking-[0.2em] opacity-50 mb-4 flex items-center gap-4">
              <span className="w-10 h-px bg-current opacity-50"></span>
              {label}
            </p>
            <h2 className={`landing-h2 ${isLight ? 'text-[var(--charcoal)]' : 'text-[var(--cream)]'}`}>{description.title}</h2>
            <div className="chapter-description mt-6 space-y-4 text-sm md:text-base opacity-85">
              {description.paragraphs.map((para: string, i: number) => (
                <p key={i}>{para}</p>
              ))}
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="chapter-content landing-chapter-content">
            <p className="chapter-label text-xs md:text-sm font-semibold uppercase tracking-[0.2em] opacity-50 mb-4 flex items-center gap-4">
              <span className="w-10 h-px bg-current opacity-50"></span>
              {label}
            </p>
            <h2 className={`landing-h2 ${isLight ? 'text-[var(--charcoal)]' : 'text-[var(--cream)]'}`}>{description.title}</h2>
            <div className="chapter-description mt-6 space-y-4 text-sm md:text-base opacity-85">
              {description.paragraphs.map((para: string, i: number) => (
                <p key={i}>{para}</p>
              ))}
            </div>
          </div>
          {children}
        </>
      )}
    </div>
  </section>
);

interface IllustrationProps {
  children: ReactNode;
}

const Illustration = ({ children }: IllustrationProps) => (
  <div className="illustration landing-illustration">{children}</div>
);

export const AnimatedLanding = () => {
  const { connected } = useWallet();

  useProgressBar();
  useHeroAnimations();
  useChapterAnimations();
  useFinaleAnimations();
  useCustomCursor();
  useSpotlightObserver();
  useNavbarHideOnScroll();

  useEffect(() => {
    // Reset scroll position when component mounts (especially after wallet disconnect)
    window.scrollTo(0, 0);

    // Refresh ScrollTrigger to recalculate positions after mount
    ScrollTrigger.refresh();

    return () => {
      // Cleanup on unmount - kill all animations and ScrollTriggers
      gsap.killTweensOf('*');
      ScrollTrigger.getAll().forEach(trigger => trigger.kill());
    };
  }, []);

  // Don't render landing page if wallet is connected
  if (connected) {
    return null;
  }

  return (
    <>
      {/* Navbar */}
      <Navbar />

      {/* Progress Bar */}
      <div className="progress-bar" />

      {/* Spotlight */}
      <div className="spotlight-layer" />

      {/* Custom Cursor */}
      <div className="cursor-outer" />
      <div className="cursor-inner" />

      {/* Hero Section */}
      <HeroSection />

      {/* Chapter 1: Vision */}
      <ChapterSection
        number={1}
        label="The Vision"
        isLight={true}
        description={{
          title: 'One identity.\nInfinite possibility.',
          paragraphs: [
            'For the first time in history, we can separate verification from control.',
            'Imagine proving who you are—once, with government-grade certainty—then carrying that proof across every service, every border, every opportunity. Without repeating KYC. Without surrendering data. Without asking permission.',
            'Your identity becomes your superpower.',
          ],
        }}
      >
        <Illustration>
          <svg viewBox="0 0 500 500" fill="none" className="w-full h-full max-w-[500px]">
            {/* Person at center - YOU (head) */}
            <circle className="fill-path" cx="250" cy="140" r="35" fill="#141413" opacity="0"/>
            {/* Person body */}
            <path className="fill-path" d="M 215 180 Q 200 220 210 280 Q 230 320 250 325 Q 270 320 290 280 Q 300 220 285 180 Q 270 170 250 170 Q 230 170 215 180"
                  fill="#141413" opacity="0"/>
            {/* Radiating lines to possibilities */}
            <path className="draw-path" d="M 250 105 L 250 50" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 280 120 L 340 80" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 290 150 L 360 150" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 280 180 L 350 220" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 265 210 L 320 280" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 235 210 L 180 280" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 220 180 L 150 220" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 210 150 L 140 150" stroke="#141413" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 220 120 L 160 80" stroke="#141413" strokeWidth="2" opacity="0"/>
            {/* Labels */}
            <text className="label-path" x="250" y="40" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Global</text>
            <text className="label-path" x="355" y="70" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Work</text>
            <text className="label-path" x="380" y="150" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Travel</text>
            <text className="label-path" x="370" y="225" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Bank</text>
            <text className="label-path" x="330" y="300" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Services</text>
            <text className="label-path" x="170" y="300" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Rights</text>
            <text className="label-path" x="130" y="225" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Health</text>
            <text className="label-path" x="145" y="150" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">Education</text>
          </svg>
        </Illustration>
      </ChapterSection>

      {/* Chapter 2: Technology */}
      <ChapterSection
        number={2}
        label="The Technology"
        isLight={false}
        description={{
          title: 'Architecture of trust.',
          paragraphs: [
            'Aadhaar verification flows through encrypted channels to the Solana blockchain, where cryptographic proofs lock your identity into an immutable record.',
            'No company holds your data. No database can be breached. You hold the keys. You grant access. You revoke it anytime.',
            'This is zero-knowledge infrastructure—proving truth without revealing secrets.',
          ],
        }}
      >
        <Illustration>
          <svg viewBox="0 0 500 500" fill="none" className="w-full h-full max-w-[500px]">
            {/* Aadhaar Document - left */}
            <path className="fill-path" d="M 60 200 L 60 260 Q 60 270 70 270 L 130 270 Q 140 270 140 260 L 140 210 Q 140 200 130 200 Z"
                  fill="#FAF9F5" opacity="0"/>
            <path className="draw-path" d="M 75 220 L 110 220" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 75 235 L 95 235" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <text className="label-path" x="100" y="295" textAnchor="middle" fontSize="11" fill="#FAF9F5" opacity="0">Aadhaar</text>

            {/* Arrow to lock */}
            <path className="draw-path" d="M 140 235 L 180 235" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>

            {/* Lock - center-left */}
            <rect className="fill-path" x="185" y="215" width="45" height="40" rx="4" fill="#FAF9F5" opacity="0"/>
            <path className="draw-path" d="M 195 215 L 195 200 Q 195 188 207 188 Q 219 188 219 200 L 219 215"
                  stroke="#FAF9F5" strokeWidth="5" opacity="0"/>
            <text className="label-path" x="207" y="280" textAnchor="middle" fontSize="11" fill="#FAF9F5" opacity="0">Encrypted</text>

            {/* Arrow to blockchain */}
            <path className="draw-path" d="M 230 235 L 270 235" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>

            {/* Blockchain blocks - center */}
            <rect className="fill-path" x="275" y="215" width="40" height="40" rx="4" fill="#FAF9F5" opacity="0"/>
            <rect className="fill-path" x="320" y="215" width="40" height="40" rx="4" fill="#FAF9F5" opacity="0"/>
            <rect className="fill-path" x="365" y="215" width="40" height="40" rx="4" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="340" y="280" textAnchor="middle" fontSize="11" fill="#FAF9F5" opacity="0">Solana</text>

            {/* Arrow to verified - curves down */}
            <path className="draw-path" d="M 340 255 Q 340 320 280 350" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>

            {/* Verified badge - bottom */}
            <circle className="fill-path" cx="250" cy="370" r="35" fill="#FAF9F5" opacity="0"/>
            <path className="draw-path" d="M 235 370 L 245 380 L 270 355" stroke="#141413" strokeWidth="4" fill="none" opacity="0"/>
            <text className="label-path" x="250" y="425" textAnchor="middle" fontSize="12" fill="#FAF9F5" opacity="0">Verified</text>
          </svg>
        </Illustration>
      </ChapterSection>

      {/* Chapter 3: Impact */}
      <ChapterSection
        number={3}
        label="The Impact"
        isLight={true}
        description={{
          title: 'Breaking barriers.\nEnabling dreams.',
          paragraphs: [
            '1.4 billion Indians gain global identity portability. 32 million NRIs prove credentials without embassy visits.',
            'Banks verify customers in 2 seconds, not 2 days. Fintechs cut KYC costs by 90%. Enterprises access verified users instantly.',
            'The estimated market: $52 billion globally. But the real value is human—financial inclusion accelerated, borders erased, opportunity democratized.',
          ],
        }}
      >
        <Illustration>
          <svg viewBox="0 0 500 500" fill="none" className="w-full h-full max-w-[500px]">
            {/* Growth trajectory curve */}
            <path className="draw-path" d="M 50 420 Q 120 400 160 350 Q 220 280 280 220 Q 340 160 400 100 Q 430 70 460 50"
                  fill="none" stroke="#141413" strokeWidth="3" opacity="0"/>

            {/* Barrier 1 - Geography */}
            <path className="draw-path barrier" d="M 140 450 L 140 280" fill="none" stroke="#141413" strokeWidth="2" strokeDasharray="10 5" opacity="0"/>
            <text className="label-path" x="140" y="470" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">Geography</text>

            {/* Barrier 2 - Bureaucracy */}
            <path className="draw-path barrier" d="M 260 400 L 260 180" fill="none" stroke="#141413" strokeWidth="2" strokeDasharray="10 5" opacity="0"/>
            <text className="label-path" x="260" y="420" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">Bureaucracy</text>

            {/* Barrier 3 - Exclusion */}
            <path className="draw-path barrier" d="M 380 320 L 380 80" fill="none" stroke="#141413" strokeWidth="2" strokeDasharray="10 5" opacity="0"/>
            <text className="label-path" x="380" y="340" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">Exclusion</text>

            {/* Milestone dots */}
            <circle className="fill-path" cx="160" cy="350" r="10" fill="#141413" opacity="0"/>
            <text className="label-path" x="160" y="380" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">1.4B Indians</text>

            <circle className="fill-path" cx="280" cy="220" r="10" fill="#141413" opacity="0"/>
            <text className="label-path" x="280" y="250" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">32M NRIs</text>

            <circle className="fill-path" cx="400" cy="100" r="10" fill="#141413" opacity="0"/>
            <text className="label-path" x="400" y="130" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">$52B Market</text>

            <circle className="fill-path" cx="460" cy="50" r="12" fill="#141413" opacity="0"/>
            <text className="label-path" x="460" y="35" textAnchor="middle" fontSize="10" fill="#141413" opacity="0">Freedom</text>
          </svg>
        </Illustration>
      </ChapterSection>

      {/* Chapter 4: Revolution */}
      <ChapterSection
        number={4}
        label="The Revolution"
        isLight={false}
        description={{
          title: 'An identity\nrenaissance.',
          paragraphs: [
            "We're not just building technology. We're catalyzing a shift in how humanity thinks about ownership of self.",
            "Your identity shouldn't be trapped in silos. It shouldn't be sold without consent. It shouldn't require endless repetition.",
            'Join us in creating the world\'s first government-grade self-sovereign identity platform—where 1.4 billion people become truly free to move, to work, to live anywhere on Earth.',
          ],
        }}
      >
        <Illustration>
          <svg viewBox="0 0 500 500" fill="none" className="w-full h-full max-w-[500px]">
            {/* Central person - YOU */}
            <circle className="fill-path" cx="250" cy="250" r="45" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="250" y="255" textAnchor="middle" fontSize="11" fill="#141413" opacity="0">YOU</text>

            {/* Inner circle - self sovereignty */}
            <circle className="draw-path" cx="250" cy="250" r="65" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>

            {/* Satellite nodes */}
            <circle className="fill-path" cx="250" cy="100" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="250" y="80" textAnchor="middle" fontSize="10" fill="#FAF9F5" opacity="0">Bank</text>

            <circle className="fill-path" cx="390" cy="170" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="425" y="175" textAnchor="start" fontSize="10" fill="#FAF9F5" opacity="0">Employer</text>

            <circle className="fill-path" cx="400" cy="300" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="410" y="340" textAnchor="start" fontSize="10" fill="#FAF9F5" opacity="0">Gov</text>

            <circle className="fill-path" cx="310" cy="410" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="310" y="450" textAnchor="middle" fontSize="10" fill="#FAF9F5" opacity="0">Hospital</text>

            <circle className="fill-path" cx="190" cy="410" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="190" y="450" textAnchor="middle" fontSize="10" fill="#FAF9F5" opacity="0">School</text>

            <circle className="fill-path" cx="100" cy="300" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="60" y="305" textAnchor="end" fontSize="10" fill="#FAF9F5" opacity="0">Telco</text>

            <circle className="fill-path" cx="110" cy="170" r="22" fill="#FAF9F5" opacity="0"/>
            <text className="label-path" x="75" y="175" textAnchor="end" fontSize="10" fill="#FAF9F5" opacity="0">Travel</text>

            {/* Connection lines from YOU to each node */}
            <path className="draw-path" d="M 250 205 L 250 122" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
            <path className="draw-path" d="M 285 235 Q 350 200 368 180" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
            <path className="draw-path" d="M 285 265 Q 350 275 378 295" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
            <path className="draw-path" d="M 270 285 Q 290 350 305 388" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
            <path className="draw-path" d="M 230 285 Q 210 350 195 388" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
            <path className="draw-path" d="M 215 265 Q 150 275 122 295" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
            <path className="draw-path" d="M 215 235 Q 150 200 132 180" fill="none" stroke="#FAF9F5" strokeWidth="1.5" opacity="0"/>
          </svg>
        </Illustration>
      </ChapterSection>

      {/* Finale */}
      <section className="finale landing-finale">
        <div className="finale-illustration w-80 h-80 md:w-96 md:h-96">
          <svg viewBox="0 0 500 500" fill="none" className="w-full h-full">
            {/* Person silhouette */}
            <circle className="fill-path" cx="250" cy="150" r="45" fill="#FAF9F5" opacity="0"/>
            <path className="fill-path" d="M 200 200 Q 175 235 185 300 Q 210 375 250 385 Q 290 375 315 300 Q 325 235 300 200 Q 275 190 250 190 Q 225 190 200 200"
                  fill="#FAF9F5" opacity="0"/>

            {/* Key shape - ownership */}
            <circle className="draw-path" cx="250" cy="320" r="30" fill="none" stroke="#FAF9F5" strokeWidth="5" opacity="0"/>
            <path className="draw-path" d="M 250 350 L 250 400" fill="none" stroke="#FAF9F5" strokeWidth="5" opacity="0"/>
            <path className="draw-path" d="M 250 380 L 275 380" fill="none" stroke="#FAF9F5" strokeWidth="5" opacity="0"/>
            <path className="draw-path" d="M 250 395 L 270 395" fill="none" stroke="#FAF9F5" strokeWidth="5" opacity="0"/>

            {/* Converging rays */}
            <path className="draw-path" d="M 250 40 Q 255 80 250 105" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 400 60 Q 340 100 300 140" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 450 250 Q 380 255 320 255" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 410 430 Q 350 370 310 330" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 250 460 Q 250 420 250 390" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 90 430 Q 150 370 190 330" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 50 250 Q 120 245 180 255" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>
            <path className="draw-path" d="M 100 60 Q 160 100 200 140" fill="none" stroke="#FAF9F5" strokeWidth="2" opacity="0"/>

            {/* Outer ring - unity */}
            <circle className="draw-path" cx="250" cy="250" r="200" fill="none" stroke="#FAF9F5" strokeWidth="1" opacity="0" strokeDasharray="12 6"/>
          </svg>
        </div>

        <div className="landing-finale-copy">
          <p className="landing-label mb-4 text-[var(--cream)] opacity-50">Ready for the future?</p>
          <h2 className="landing-h2 mb-8 text-[var(--cream)]">
            Own Your Identity.
            <br />
            Change Everything.
          </h2>
          <p className="text-base md:text-lg opacity-90 mb-8">
            AadhaarChain is bringing self-sovereign identity to India and the world.
            <br />
            The revolution starts with you.
          </p>
          <Button asChild size="lg" className="bg-background text-foreground hover:bg-card">
            <Link href="/identity/create">Join the revolution</Link>
          </Button>
        </div>
      </section>
    </>
  );
};
