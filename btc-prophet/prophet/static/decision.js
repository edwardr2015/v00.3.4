'use strict';

function renderDirection(card, tally, data) {
  const side = data?.call?.decision;
  const known = side === 'UP' || side === 'DOWN';
  const pending = window.prophetPending || 'Waiting';
  card.textContent = known ? side : pending;
  card.dataset.direction = known ? side : (pending === 'SKIP' ? 'skip' : 'waiting');
  tally.hidden = !known || !data.count;
  tally.textContent = tally.hidden ? '' :
    `Last ${data.count} of up to 20 calls · UP ${data.up_percent.toFixed(0)}% (${data.up_count}) · DOWN ${data.down_percent.toFixed(0)}% (${data.down_count})`;
}

function renderBenchmark(element, result) {
  const heading = '$1 momentum · +90s paper benchmark';
  if (!result || result.start == null) {
    element.textContent = `${heading} · Experiment not started`;
    return;
  }
  const money = new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(result.pnl);
  element.textContent = `${heading} · Since start: ${result.wins} wins, ${result.losses} losses` +
    (result.flats ? `, ${result.flats} even` : '') +
    ` · ${money} net · Longest streaks: ${result.longest_win} wins / ${result.longest_loss} losses` +
    (result.current ? ` · Current: ${result.streak} ${result.current.toLowerCase()}` : '');
}
