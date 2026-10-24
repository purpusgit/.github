// Colour shades and dialogs are not viewport units.
final c = Colors.blue.shade100;
void open(BuildContext ctx) => showDialog(context: ctx, builder: (_) => const Text('x')).show;
final h = MediaQuery.of(context).size.height;
