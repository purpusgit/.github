const A = `
  SELECT o.id
  FROM meta.organization o
  LEFT JOIN taxo.master opc ON opc.id = o.org_purpose_category_idfr
    AND opc.type = 'match_purpose' AND opc.hierarchy_level = 'domain' AND opc.is_active = 1
  LEFT JOIN taxo.master omc ON omc.id = o.org_mission_category_idfr
    AND omc.type = 'match_mission' AND omc.hierarchy_level = 'domain'
  WHERE o.is_deleted = 0;
`;
