export class X {
    // TAXO_CONTRACT: o_department@leaf
    private async validateDeptId(deptId: number): Promise<boolean> {
        const query = `SELECT COUNT(*) as count FROM taxo.master WHERE id = ${escape(deptId)} AND type = 'o_department' AND hierarchy_level = 'domain' AND is_deleted = 0 AND is_active = 1`;
        return true;
    }
}
