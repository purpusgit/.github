const Q = `
  SELECT o.id
  FROM meta.organization o
  LEFT JOIN taxo.master AS od ON od.id = o.org_department_idfr AND od.type = 'o_department' AND od.hierarchy_level = 'leaf' AND od.is_deleted = 0 AND od.is_active = 1
  LEFT JOIN taxo.master AS ic ON ic.id = o.influencer_category_idfr AND ic.type = 'infl_domain' AND ic.is_deleted = 0
`;
