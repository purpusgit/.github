// PASS fixture for reusable-tsc-check.yml, step "TypeScript type-check".
// Type-correct: `tsc --noEmit` must exit 0 here.
export const add = (a: number, b: number): number => a + b;

export const total: number = add(1, 2);
