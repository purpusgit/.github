// Fixture for reusable-taxo-lint.yml's --code half. The layout is `caller/<code_dir>`
// because the gate checks out the consumer repo into `caller/` and taxo_lint.py into
// `gate/`, then runs `--code "caller/${{ inputs.code_dir }}"` (default code_dir: src).
export const orgDepartmentsQuery = `
  SELECT m.identifier, m.value
  FROM taxo.master m
  WHERE m.type = 'org_department'
    AND m.parent_identifier = ?
    AND m.is_deleted = 0
`;
