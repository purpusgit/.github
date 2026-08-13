const A = `
  SELECT id FROM taxo.master
  WHERE org_department_idfr = 9
  AND type = 'o_department' AND hierarchy_level = 'leaf';
`;
