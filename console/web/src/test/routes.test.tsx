import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { NotBuiltYet } from '../app/NotBuiltYet';
import {
  NAV_GROUPS,
  ROUTES,
  pageModuleCandidates,
  pageModulePaths,
  pageModuleKey,
  resolvePageComponent,
  routeById,
  routeByPath,
} from '../routes';
import { renderWithProviders } from './utils';

describe('route registry', () => {
  it('declares the 21 pages of the spec', () => {
    expect(ROUTES).toHaveLength(21);
  });

  it('has unique ids and paths', () => {
    expect(new Set(ROUTES.map((route) => route.id)).size).toBe(ROUTES.length);
    expect(new Set(ROUTES.map((route) => route.path)).size).toBe(ROUTES.length);
  });

  it('puts every route in a known navigation group and gives it an owner', () => {
    for (const route of ROUTES) {
      expect(NAV_GROUPS).toContain(route.group);
      expect(route.owner).toMatch(/^P\d$/);
      expect(route.path.startsWith('/')).toBe(true);
      expect(route.description.length).toBeGreaterThan(10);
    }
  });

  it('covers spec 12 pages 1..21 exactly once', () => {
    expect([...ROUTES].map((route) => route.page).sort((a, b) => a - b)).toEqual(
      Array.from({ length: 21 }, (_, index) => index + 1),
    );
  });

  it('lists the groups in order and the pages inside a group in spec 12 order', () => {
    // The navigation renders ROUTES filtered by group, so the array order *is* the
    // navigation order — a page inserted in the wrong place shows up in the wrong place.
    const groupsInOrder = ROUTES.map((route) => route.group).filter(
      (group, index, all) => group !== all[index - 1],
    );
    expect(groupsInOrder).toEqual(NAV_GROUPS);

    for (const group of NAV_GROUPS) {
      const pages = ROUTES.filter((route) => route.group === group).map((route) => route.page);
      expect(pages, `${group} is not in spec 12 page order`).toEqual([...pages].sort((a, b) => a - b));
    }
  });

  it('serves the overview at the root', () => {
    expect(routeByPath('/')?.id).toBe('overview');
    expect(routeById('invariants')?.path).toBe('/invariants');
  });

  it('anchors the page glob at /src/pages so the lookup keys match', () => {
    expect(pageModuleCandidates('overview')).toEqual([
      '/src/pages/overview/index.tsx',
      '/src/pages/overview.tsx',
      '/src/pages/overview/OverviewPage.tsx',
    ]);
    expect(pageModuleCandidates('self-improvement')[2]).toBe(
      '/src/pages/self-improvement/SelfImprovementPage.tsx',
    );
    for (const path of pageModulePaths()) {
      expect(path.startsWith('/src/pages/')).toBe(true);
    }
  });

  it('resolves at least one route when page modules are present', () => {
    if (pageModulePaths().length === 0) return; // clean checkout: no feature pages yet
    const resolved = ROUTES.filter((route) => pageModuleKey(route.id) !== null);
    expect(resolved.length).toBeGreaterThan(0);
    for (const route of resolved) {
      expect(resolvePageComponent(route.id)).not.toBeNull();
    }
  });

  it('falls back to the placeholder until a package ships the page module', () => {
    for (const route of ROUTES) {
      if (pageModuleKey(route.id) === null) {
        expect(resolvePageComponent(route.id)).toBeNull();
      }
    }
  });

  it('renders the placeholder with the owning package', () => {
    const route = routeById('skills');
    expect(route).toBeDefined();
    renderWithProviders(<NotBuiltYet route={route!} />);
    expect(screen.getByTestId('not-built-skills')).toHaveTextContent('P6');
    expect(screen.getByText('Not built yet')).toBeInTheDocument();
  });
});
