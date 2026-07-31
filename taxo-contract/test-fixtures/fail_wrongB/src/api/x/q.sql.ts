const A = `
  SELECT id FROM taxo.master
  WHERE influencer_category_idfr = 5
  AND type = 'infl_domain' AND hierarchy_level = 'category';
`;
const B = `
  SELECT id FROM taxo.master
  WHERE org_department_idfr = 9
  AND type = 'o_department' AND hierarchy_level = 'realm';
`;
