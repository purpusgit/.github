// FAIL fixture for reusable-tsc-check.yml, step "TypeScript type-check".
// Plants TS2322 (number not assignable to string). The harness asserts on the
// diagnostic code, not just a non-zero exit, so a missing/broken `npx` (exit
// 127) cannot be mistaken for "the gate caught the violation".
export const add = (a: number, b: number): number => a + b;

export const label: string = add(1, 2);
