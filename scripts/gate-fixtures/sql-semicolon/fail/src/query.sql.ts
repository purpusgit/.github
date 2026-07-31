export const q = `
  SELECT id FROM users
  WHERE active = 1;
    AND deleted = 0
`;
