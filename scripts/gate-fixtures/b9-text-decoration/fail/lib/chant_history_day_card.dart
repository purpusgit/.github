// REAL, NOT SYNTHETIC. This is pkg_orbit_japa's Chant History day-card headline
// as it stood on `cwb` at 39c4d2f — the code that shipped yellow underlines to a
// devotee's phone. The pass/ fixture is the same file after the one-line fix, so
// what this pair asserts is unambiguously the predicate and not a difference in
// how the two were written.
//
// The comment below is deliberately kept: it says "decoration" in prose, and a
// gate that scored a COMMENT as a neutraliser would pass this file. It must not.
// Rule B9 forbids inheriting a decoration through a span tree.
import 'package:flutter/material.dart';

class DayCard extends StatelessWidget {
  const DayCard({super.key});

  @override
  Widget build(BuildContext context) {
    return RichText(
      maxLines: 1,
      overflow: TextOverflow.ellipsis,
      text: TextSpan(
        style: DefaultTextStyle.of(context).style,
        children: const <InlineSpan>[
          TextSpan(text: '7 Rounds', style: TextStyle(fontSize: 19)),
          TextSpan(text: '  '),
          TextSpan(text: '12 Beads', style: TextStyle(fontSize: 13)),
        ],
      ),
    );
  }
}
