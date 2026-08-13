const q = `SELECT COUNT(*) as count FROM taxo.master
WHERE id = ${escape(department_idfr)}
AND type = 'o_department'
AND hierarchy_level = 'domain'
AND is_deleted = 0 AND is_active = 1`;
