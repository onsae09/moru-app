/**
 * Root container class shared by every wizard step (W1–W6) so width, padding
 * and entry animation match from step to step. W3's sticky footer bleeds over
 * this padding (-mx-10 / -mb-8); keep them in sync.
 */

export const WIZARD_PAGE_CLASS = "w-full max-w-[1280px] animate-fade-in-up px-10 py-8";
