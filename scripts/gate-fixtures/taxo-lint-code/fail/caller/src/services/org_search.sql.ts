// PLANTED VIOLATION — both recurring shapes this gate exists to catch, exactly as
// they arrive in real code:
//   G1 (Rule 59) a PascalCase taxo.master type literal, e.g. 'Org_Department'
//   G2           a taxo.master column that is not in the real schema, e.g. parent_idfr
export const orgDepartmentsQuery = `
  SELECT m.identifier, m.value
  FROM taxo.master m
  WHERE m.type = 'Org_Department'
    AND m.parent_idfr = ?
    AND m.is_deleted = 0
`;
