type MessageKind = 'System_Notice' | 'User_Message';
export async function lookupClean() {
  const SQL = `SELECT id FROM taxo.master WHERE type = 'o_department'`;
  return pool.query(SQL);
}
export async function lookupBad() {
  const SQL2 = `SELECT id FROM taxo.master WHERE type = 'Org_Department'`;
  return pool.query(SQL2);
}
