/// <reference types="vite/client" />

/*
 * React 19 removed the global `JSX` namespace in favour of `React.JSX`.  The shell used to
 * alias it back for feature pages that annotated `JSX.Element`; the only such annotations
 * were in the dead `src/pages/*\/routes.tsx` files, so the alias is gone with them.  A page
 * that needs the type imports `JSX` from `react`.
 */
export {};
