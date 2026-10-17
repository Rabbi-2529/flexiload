# Offer Check — Laravel Implementation

A drop-in **Offer Check** feature for your Laravel panel: select an operator, enter a
number and amount, fetch the available offers from the aggregator API, filter them,
and buy an offer. This mirrors exactly the API contract used in the working
Django implementation — same endpoints, same operator codes, same offer fields.

Nothing here changes your existing project logic. It adds one controller, one Blade
view, two routes, and a config block.

---

## 1. Aggregator API contract

Base URL: `http://YOUR_SERVER_IP:8080` *(the Laravel reference originally used `:9090` —
keep it configurable in `.env`)*

| Operator | Endpoint | `operator` code | Extra query |
|----------|----------|-----------------|-------------|
| Grameenphone | `GET /api/powerload` | `grameen` | `type=offer` |
| Robi | `GET /api/offer` | `rb` | — |
| Airtel | `GET /api/offer` | `at` | — |
| Banglalink | `GET /api/offer` | `bl` | — |
| Teletalk | *not supported* | — | — |

**Every request sends these query parameters:**

```
user_id   = YOUR_USER_ID
password  = YOUR_PASSWORD
number    = 01XXXXXXXXX      (the subscriber number)
amount    = 10               (integer)
refer_id  = YOUR_REFER_ID
operator  = grameen|rb|at|bl
type      = offer            (ONLY for /api/powerload)
```

Example (Banglalink):

```
GET http://YOUR_SERVER_IP:8080/api/offer?user_id=YOUR_USER_ID&password=YOUR_PASSWORD&number=01911223344&amount=19&refer_id=YOUR_REFER_ID&operator=bl
```

Example (Grameenphone):

```
GET http://YOUR_SERVER_IP:8080/api/powerload?user_id=YOUR_USER_ID&password=YOUR_PASSWORD&number=01711223344&amount=29&refer_id=YOUR_REFER_ID&operator=grameen&type=offer
```

### Response offer fields (differ per operator)

The API returns the offer list under one of: `offers`, `data`, `offerList`,
`list`, `result`, `offersList` (or the response is a plain array). Each offer's
fields depend on the operator — map them like this:

| Field in UI | Banglalink (`bl`) | Robi (`rb`) / Airtel (`at`) | Grameenphone (`grameen`) |
|-------------|-------------------|------------------------------|---------------------------|
| id | `offerId` / `id` / `packageId` | `ticketId` / `offerId` | `offerId` |
| amount | `amount` / `price` / `offerPrice` | `amount` | `offerPrice` |
| summary | `offerSummary` / `description` / `packageName` | `subscriberOfferMessage` | `offerDescription` |
| validity | `validity` / `offerValidity` / `duration` | `validity` | `offerValidityHours` (append `H`) |
| ussd code | `ussdCode` / `optInKeyword` / `code` | `ussdCode` | `optInKeyword` |
| type | `offerType` / `category` | `offerType` | `offerContentType` |

---

## 2. Config

Add to **`.env`**:

```dotenv
OFFER_API_BASE_URL=http://YOUR_SERVER_IP:8080
OFFER_API_USER_ID=YOUR_USER_ID
OFFER_API_PASSWORD=YOUR_PASSWORD
OFFER_API_REFER_ID=YOUR_REFER_ID
OFFER_API_TIMEOUT=30
```

Add to **`config/services.php`** (inside the returned array):

```php
'offer_api' => [
    'base_url'  => env('OFFER_API_BASE_URL', 'http://YOUR_SERVER_IP:8080'),
    'user_id'   => env('OFFER_API_USER_ID', 'YOUR_USER_ID'),
    'password'  => env('OFFER_API_PASSWORD', 'YOUR_PASSWORD'),
    'refer_id'  => env('OFFER_API_REFER_ID', 'YOUR_REFER_ID'),
    'timeout'   => (int) env('OFFER_API_TIMEOUT', 30),
],
```

---

## 3. Routes

Add to **`routes/web.php`** (adjust the middleware group / prefix to match your panel):

```php
use App\Http\Controllers\OfferController;

Route::middleware(['auth'])->group(function () {
    Route::get('/offers',       [OfferController::class, 'offerForm'])->name('offers.form');
    Route::post('/offers/check',[OfferController::class, 'checkOffer'])->name('offers.check');
    Route::post('/offers/buy',  [OfferController::class, 'buyOfferProcess'])->name('offers.buy');
});
```

If you also want a JSON endpoint (for mobile / AJAX), add to **`routes/api.php`**:

```php
Route::middleware('auth:sanctum')->post('/offers/check', [OfferController::class, 'checkOfferApi']);
```

---

## 4. Controller — `app/Http/Controllers/OfferController.php`

```php
<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;

class OfferController extends Controller
{
    /**
     * UI operator value => [endpoint, api operator code]
     */
    private array $operatorMap = [
        'grameen' => ['powerload', 'grameen'],
        'gp'      => ['powerload', 'grameen'],
        'rb'      => ['offer', 'rb'],
        'robi'    => ['offer', 'rb'],
        'at'      => ['offer', 'at'],
        'airtel'  => ['offer', 'at'],
        'bl'      => ['offer', 'bl'],
        'banglalink' => ['offer', 'bl'],
    ];

    private array $operatorChoices = [
        'grameen'  => 'Grameenphone',
        'rb'       => 'Robi',
        'at'       => 'Airtel',
        'bl'       => 'Banglalink',
        'teletalk' => 'Teletalk',
    ];

    private array $categoryLabels = [
        'all'       => 'All Offers',
        '30days'    => '30 Days',
        '7days'     => '7 Days',
        'data'      => 'Data',
        'minutes'   => 'Minutes',
        'combo'     => 'Combo',
        'social'    => 'Social',
        'streaming' => 'Streaming',
        'unlimited' => 'Unlimited',
    ];

    /** Show the empty form. */
    public function offerForm()
    {
        return view('offers.check', [
            'operators'  => $this->operatorChoices,
            'categories' => $this->categoryLabels,
            'form'       => ['operator' => 'bl', 'number' => '', 'amount' => '10'],
            'result'     => null,
            'error'      => null,
        ]);
    }

    /** Handle the form submit and fetch offers. */
    public function checkOffer(Request $request)
    {
        $form = [
            'operator' => trim((string) $request->input('operator')),
            'number'   => trim((string) $request->input('number')),
            'amount'   => trim((string) $request->input('amount', '10')),
        ];

        $result = null;
        $error  = null;

        try {
            $result = $this->fetchOffers($form['operator'], $form['number'], $form['amount']);
        } catch (\Throwable $e) {
            $error = $e->getMessage();
        }

        return view('offers.check', [
            'operators'  => $this->operatorChoices,
            'categories' => $this->categoryLabels,
            'form'       => $form,
            'result'     => $result,
            'error'      => $error,
        ]);
    }

    /** JSON endpoint (optional). */
    public function checkOfferApi(Request $request)
    {
        try {
            $result = $this->fetchOffers(
                (string) $request->input('operator'),
                (string) $request->input('number'),
                $request->input('amount', 10)
            );
        } catch (\Throwable $e) {
            return response()->json(['error' => $e->getMessage()], 400);
        }

        return response()->json([
            'operator' => $request->input('operator'),
            'number'   => $request->input('number'),
            'amount'   => $request->input('amount'),
            'count'    => $result['counts']['all'],
            'offers'   => $result['offers'],
        ]);
    }

    /**
     * Core: call the aggregator and return normalized offers + counts + filters.
     *
     * @throws \InvalidArgumentException on bad input
     * @throws \RuntimeException on API/network problems
     */
    private function fetchOffers(string $uiOperator, string $number, $amount): array
    {
        $uiOperator = strtolower(trim($uiOperator));
        $number     = trim($number);

        if (!preg_match('/^(?:\+?88)?01[3-9]\d{8}$/', $number)) {
            throw new \InvalidArgumentException('Enter a valid Bangladesh mobile number.');
        }

        $amount = (int) $amount;
        if ($amount <= 0) {
            throw new \InvalidArgumentException('Enter a valid amount.');
        }

        if (!isset($this->operatorMap[$uiOperator])) {
            throw new \InvalidArgumentException('Offer check is not supported for this operator.');
        }

        [$endpoint, $apiOperator] = $this->operatorMap[$uiOperator];

        $cfg   = config('services.offer_api');
        $query = [
            'user_id'  => $cfg['user_id'],
            'password' => $cfg['password'],
            'number'   => $number,
            'amount'   => $amount,
            'refer_id' => $cfg['refer_id'],
            'operator' => $apiOperator,
        ];
        if ($endpoint === 'powerload') {
            $query['type'] = 'offer';
        }

        $url = rtrim($cfg['base_url'], '/') . '/api/' . $endpoint;

        try {
            $response = Http::timeout($cfg['timeout'])
                ->acceptJson()
                ->withHeaders(['User-Agent' => 'Flexiload-Laravel/1.0'])
                ->get($url, $query);
        } catch (\Throwable $e) {
            Log::error('Offer API request failed for ' . $apiOperator . ': ' . $e->getMessage());
            throw new \RuntimeException('Offer API request failed or timed out. Please try again.');
        }

        if ($response->failed()) {
            Log::error('Offer API HTTP ' . $response->status() . ' for ' . $apiOperator . ': ' . substr($response->body(), 0, 300));
            throw new \RuntimeException('Offer API error (' . $response->status() . '). Please try again.');
        }

        $apiResponse = $response->json();
        if ($apiResponse === null) {
            throw new \RuntimeException('Offer API returned an unreadable response.');
        }

        $rawOffers = $this->extractOffers($apiResponse);
        $offers    = [];
        foreach ($rawOffers as $raw) {
            $n = $this->normalizeOffer($uiOperator, $raw);
            if ($n !== null) {
                $offers[] = $n;
            }
        }

        // Count per category.
        $counts = array_fill_keys(array_keys($this->categoryLabels), 0);
        $counts['all'] = count($offers);
        foreach ($offers as $offer) {
            foreach ($offer['cat'] as $key => $isOn) {
                if ($isOn) {
                    $counts[$key]++;
                }
            }
        }

        $filters = [];
        foreach ($this->categoryLabels as $key => $label) {
            $filters[] = ['key' => $key, 'label' => $label, 'count' => $counts[$key] ?? 0];
        }

        $message = is_array($apiResponse) ? ($apiResponse['message'] ?? '') : '';

        Log::info("Offer check operator={$apiOperator} number=" . substr($number, 0, 5) . '*** amount=' . $amount . ' offers=' . count($offers));

        return [
            'offers'     => $offers,
            'counts'     => $counts,
            'filters'    => $filters,
            'message'    => $message,
            'uiOperator' => $uiOperator,
        ];
    }

    /** Pull the offer array out of whatever shape the API returns. */
    private function extractOffers($apiResponse): array
    {
        if (array_is_list((array) $apiResponse) && is_array($apiResponse)) {
            return $apiResponse;
        }
        foreach (['offers', 'data', 'offerList', 'list', 'result', 'offersList'] as $key) {
            if (isset($apiResponse[$key]) && is_array($apiResponse[$key])) {
                return $apiResponse[$key];
            }
        }
        return [];
    }

    /** First non-empty value among $keys. */
    private function first(array $map, array $keys, $default = '')
    {
        foreach ($keys as $key) {
            if (isset($map[$key]) && $map[$key] !== '' && $map[$key] !== null) {
                return $map[$key];
            }
        }
        return $default;
    }

    /** Map one raw offer to the UI shape, per operator. */
    private function normalizeOffer(string $op, $offer): ?array
    {
        if (!is_array($offer)) {
            return null;
        }

        if (in_array($op, ['bl', 'banglalink'], true)) {
            $id       = $this->first($offer, ['offerId', 'id', 'offer_id', 'packageId', 'package_id']);
            $amount   = $this->first($offer, ['amount', 'price', 'offerPrice', 'offer_price', 'packagePrice', 'package_price'], '0');
            $summary  = $this->first($offer, ['offerSummary', 'summary', 'offerDescription', 'description', 'name', 'packageName', 'package_name'], 'Banglalink Offer');
            $validity = $this->first($offer, ['validity', 'offerValidity', 'validityHours', 'offerValidityHours', 'duration', 'validity_days'], 'N/A');
            $ussd     = $this->first($offer, ['ussdCode', 'ussd', 'optInKeyword', 'code', 'ussdString', 'ussd_code']);
            $type     = $this->first($offer, ['offerType', 'type', 'offerContentType', 'category', 'offer_type'], 'Offer');
        } elseif (in_array($op, ['rb', 'robi', 'at', 'airtel'], true)) {
            $id       = $this->first($offer, ['ticketId', 'offerId']);
            $amount   = $this->first($offer, ['amount'], 0);
            $summary  = $this->first($offer, ['subscriberOfferMessage', 'offerSummary'], ($op === 'rb' || $op === 'robi') ? 'Robi Offer' : 'Airtel Offer');
            $validity = $this->first($offer, ['validity'], 'N/A');
            $ussd     = $this->first($offer, ['ussdCode']);
            $type     = $this->first($offer, ['offerType'], 'Offer');
        } elseif (in_array($op, ['grameen', 'gp', 'grameenphone'], true)) {
            $id            = $this->first($offer, ['offerId']);
            $amount        = $this->first($offer, ['offerPrice'], '0');
            $summary       = $this->first($offer, ['offerDescription'], 'N/A');
            $validityHours = $this->first($offer, ['offerValidityHours'], '');
            $validity      = $validityHours !== '' ? ($validityHours . 'H') : 'N/A';
            $ussd          = $this->first($offer, ['optInKeyword']);
            $type          = $this->first($offer, ['offerContentType'], '');
        } else {
            $id       = $this->first($offer, ['offerId']);
            $amount   = $this->first($offer, ['amount'], '0');
            $summary  = $this->first($offer, ['offerSummary'], 'N/A');
            $validity = $this->first($offer, ['validity'], 'N/A');
            $ussd     = $this->first($offer, ['ussdCode']);
            $type     = $this->first($offer, ['offerType'], '');
        }

        $summary = $this->cleanSummary($summary);

        $amountNum = is_numeric($amount) ? (float) $amount : 0;
        if ($amountNum == (int) $amountNum) {
            $amountNum = (int) $amountNum;
        }

        // Derive validity from the text if missing (Robi/Airtel).
        if ($validity === '' || $validity === 'N/A') {
            if (preg_match('/(\d+)\s*(day|days|hour|hours)/i', $summary, $m)) {
                $n    = (int) $m[1];
                $unit = stripos($m[2], 'day') === 0 ? 'Day' : 'Hour';
                $validity = $n . ' ' . $unit . ($n > 1 ? 's' : '');
            }
        }

        return [
            'id'          => (string) $id,
            'amount'      => $amountNum,
            'summary'     => $summary,
            'validity'    => $validity,
            'ussd'        => (string) $ussd,
            'type'        => (string) $type,
            'cat'         => $this->categories($summary, $validity),
        ];
    }

    private function cleanSummary($summary): string
    {
        $summary = preg_replace('/C-\S*/', '', (string) $summary);
        $summary = trim($summary);
        if (strpos($summary, '|') !== false) {
            $summary = trim(explode('|', $summary, 2)[0]);
        }
        return $summary !== '' ? $summary : 'Special Offer';
    }

    private function categories(string $summary, $validity): array
    {
        $s = strtolower($summary);
        $v = strtolower((string) $validity);

        $cat = [
            '30days' => (str_contains($s, '30 day') || str_contains($s, '30days') || str_contains($s, '30d') || str_contains($v, '30')),
            '7days'  => (str_contains($s, '7 day') || str_contains($s, '7days') || str_contains($s, '7d') || str_contains($v, '7')),
            'data'   => (str_contains($s, 'gb') || str_contains($s, 'data') || str_contains($s, 'internet') || str_contains($s, 'mb')),
            'minutes'=> (str_contains($s, 'min') || str_contains($s, 'minute') || str_contains($s, 'call') || str_contains($s, 'voice')),
            'social' => $this->anyWord($s, ['facebook', 'whatsapp', 'messenger', 'instagram', 'imo', 'viber', 'social']),
            'streaming' => $this->anyWord($s, ['youtube', 'tiktok', 'snapchat', 'likee', 'streaming', 'video']),
            'unlimited' => str_contains($s, 'unlimited'),
        ];
        $cat['combo'] = ($cat['data'] && $cat['minutes']) || str_contains($s, 'combo') || str_contains($s, 'both');
        if ($cat['combo']) {
            $cat['data'] = true;
            $cat['minutes'] = true;
        }
        return $cat;
    }

    private function anyWord(string $haystack, array $words): bool
    {
        foreach ($words as $w) {
            if (str_contains($haystack, $w)) {
                return true;
            }
        }
        return false;
    }

    /**
     * Buy an offer. Wire the body of this to YOUR existing recharge flow —
     * the same service/queue your panel already uses to send a top-up.
     */
    public function buyOfferProcess(Request $request)
    {
        $data = $request->validate([
            'operator' => 'required|string',
            'number'   => 'required|string',
            'amount'   => 'required|numeric|min:1',
            'product'  => 'nullable|in:prepaid,postpaid',
        ]);

        $uiOperator = strtolower(trim($data['operator']));
        if (!isset($this->operatorMap[$uiOperator])) {
            return back()->with('error', 'This operator is not supported for buying.');
        }

        // >>> Call your existing recharge service here <<<
        // Example:
        // $txn = app(RechargeService::class)->send([
        //     'operator' => $uiOperator,
        //     'number'   => $data['number'],
        //     'amount'   => (int) $data['amount'],
        //     'product'  => $data['product'] ?? 'prepaid',
        // ]);

        return back()->with('success',
            'Offer queued: BDT ' . (int) $data['amount'] . ' to ' . $data['number'] . '. Watch the transaction for the result.'
        );
    }
}
```

> **Buy step:** `buyOfferProcess` deliberately leaves the actual send to *your*
> existing recharge service — just replace the commented block with the call your
> panel already uses. In the Django version this is `execute_topup`, which queues
> the recharge; your Laravel panel has its own equivalent.

---

## 5. Blade view — `resources/views/offers/check.blade.php`

```blade
@extends('layouts.app')  {{-- change to your panel's layout --}}

@section('content')
<div class="container">
  <div class="card">
    <div class="card-body">
      <h4>Check offers for a number</h4>
      <p class="text-muted">
        Fetches available offers from the operator for this number and amount.
        Pick an offer and press Buy to send it through the recharge queue.
      </p>

      <form method="POST" action="{{ route('offers.check') }}" class="row g-3">
        @csrf
        <div class="col-md-3">
          <label class="form-label">Operator</label>
          <select name="operator" class="form-select">
            @foreach ($operators as $value => $label)
              <option value="{{ $value }}" {{ ($form['operator'] ?? '') === $value ? 'selected' : '' }}>{{ $label }}</option>
            @endforeach
          </select>
        </div>
        <div class="col-md-4">
          <label class="form-label">Mobile number</label>
          <input type="text" name="number" class="form-control" value="{{ $form['number'] ?? '' }}" placeholder="01XXXXXXXXX" inputmode="numeric">
        </div>
        <div class="col-md-2">
          <label class="form-label">Amount</label>
          <input type="number" name="amount" class="form-control" value="{{ $form['amount'] ?? '10' }}" min="1">
        </div>
        <div class="col-md-3 d-flex align-items-end">
          <button class="btn btn-primary w-100" type="submit">Check offers</button>
        </div>
      </form>

      @if (!empty($error))
        <div class="alert alert-danger mt-3">{{ $error }}</div>
      @endif
    </div>
  </div>

  @if (!empty($result))
    <div class="card mt-3">
      <div class="card-body">
        <div class="d-flex gap-4 mb-3">
          <div><small class="text-muted d-block">Operator</small><strong>{{ strtoupper($form['operator']) }}</strong></div>
          <div><small class="text-muted d-block">Number</small><strong>{{ $form['number'] }}</strong></div>
          <div><small class="text-muted d-block">Total offers</small><strong>{{ $result['counts']['all'] }}</strong></div>
        </div>

        @if (count($result['offers']))
          <div class="d-flex flex-wrap gap-2 mb-3" id="offer-filters">
            @foreach ($result['filters'] as $f)
              <button type="button"
                      class="btn btn-sm filter-btn {{ $f['key'] === 'all' ? 'btn-primary active' : 'btn-outline-secondary' }}"
                      data-filter="{{ $f['key'] }}">
                {{ $f['label'] }} ({{ $f['count'] }})
              </button>
            @endforeach
          </div>
          <div class="text-muted mb-2" id="offer-filter-status">Showing all {{ $result['counts']['all'] }} offers</div>

          <div class="row g-3" id="offer-grid">
            @foreach ($result['offers'] as $offer)
              <div class="col-md-3 offer-item"
                   data-all="yes"
                   @foreach (['30days','7days','data','minutes','combo','social','streaming','unlimited'] as $c)
                     data-{{ $c }}="{{ ($offer['cat'][$c] ?? false) ? 'yes' : 'no' }}"
                   @endforeach>
                <div class="card h-100">
                  <div class="card-body d-flex flex-column">
                    <div class="d-flex justify-content-between align-items-center">
                      <span class="badge bg-success fs-6">৳ {{ $offer['amount'] }}</span>
                      @if (!empty($offer['highlighted']))<span>🔥</span>@endif
                    </div>
                    <div class="fw-bold my-2">{{ $offer['summary'] }}</div>
                    <div class="d-flex gap-2 flex-wrap mb-2">
                      <span class="badge bg-warning text-dark">{{ $offer['validity'] }}</span>
                      @if (!empty($offer['type']))<span class="badge bg-light text-muted">{{ $offer['type'] }}</span>@endif
                    </div>
                    <button type="button" class="btn btn-primary btn-sm mt-auto offer-buy-btn"
                            data-amount="{{ $offer['amount'] }}"
                            data-summary="{{ $offer['summary'] }}"
                            data-validity="{{ $offer['validity'] }}"
                            data-ussd="{{ $offer['ussd'] }}">Buy now</button>
                  </div>
                </div>
              </div>
            @endforeach
          </div>
        @elseif (!empty($result['message']))
          <div class="alert alert-info">{{ $result['message'] }}</div>
        @else
          <div class="alert alert-secondary">No offers found for this number.</div>
        @endif
      </div>
    </div>

    {{-- Buy modal --}}
    <div class="modal fade" id="buyModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <form method="POST" action="{{ route('offers.buy') }}">
            @csrf
            <div class="modal-header">
              <strong id="m-summary">Offer</strong>
              <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
              <div class="d-flex gap-4 mb-3">
                <div><small class="text-muted d-block">Price</small><strong id="m-price"></strong></div>
                <div><small class="text-muted d-block">Validity</small><span id="m-validity"></span></div>
                <div><small class="text-muted d-block">USSD</small><span id="m-ussd"></span></div>
              </div>
              <input type="hidden" name="operator" value="{{ $form['operator'] }}">
              <input type="hidden" name="amount" id="m-amount">
              <label class="form-label">Mobile number</label>
              <input type="text" name="number" id="m-number" class="form-control mb-2" value="{{ $form['number'] }}" readonly>
              <label class="form-label">Number type</label>
              <select name="product" class="form-select">
                <option value="prepaid">Prepaid</option>
                <option value="postpaid">Postpaid</option>
              </select>
            </div>
            <div class="modal-footer">
              <button type="submit" class="btn btn-primary">Confirm buy</button>
            </div>
          </form>
        </div>
      </div>
    </div>
  @endif
</div>
@endsection

@push('scripts')
<script>
(function () {
  const grid = document.getElementById('offer-grid');
  if (!grid) return;
  const cards   = Array.from(grid.querySelectorAll('.offer-item'));
  const buttons = Array.from(document.querySelectorAll('.filter-btn'));
  const statusEl = document.getElementById('offer-filter-status');
  const active = new Set(['all']);

  function apply() {
    const selected = Array.from(active).filter(f => f !== 'all');
    let visible = 0;
    cards.forEach(card => {
      let show = true;
      if (selected.length) {
        for (const f of selected) {
          if (card.getAttribute('data-' + f) !== 'yes') { show = false; break; }
        }
        if (show && selected.includes('data') && selected.includes('minutes')) {
          if (card.getAttribute('data-combo') !== 'yes') show = false;
        } else if (show && selected.includes('data') && !selected.includes('minutes')) {
          if (card.getAttribute('data-combo') === 'yes') show = false;
        } else if (show && selected.includes('minutes') && !selected.includes('data')) {
          if (card.getAttribute('data-combo') === 'yes') show = false;
        }
      }
      card.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    statusEl.textContent = selected.length
      ? ('Showing ' + visible + ' offers matching: ' + selected.join(' + '))
      : ('Showing all ' + cards.length + ' offers');
    buttons.forEach(b => {
      const on = active.has(b.dataset.filter);
      b.classList.toggle('active', on);
      b.classList.toggle('btn-primary', on);
      b.classList.toggle('btn-outline-secondary', !on);
    });
  }

  buttons.forEach(b => b.addEventListener('click', () => {
    const f = b.dataset.filter;
    if (f === 'all') { active.clear(); active.add('all'); }
    else {
      active.delete('all');
      if (active.has(f)) { active.delete(f); if (!active.size) active.add('all'); }
      else active.add(f);
    }
    apply();
  }));
  apply();

  // Buy modal (Bootstrap 5)
  const modalEl = document.getElementById('buyModal');
  const modal = modalEl ? new bootstrap.Modal(modalEl) : null;
  document.querySelectorAll('.offer-buy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('m-summary').textContent = btn.dataset.summary;
      document.getElementById('m-price').textContent = '৳ ' + btn.dataset.amount;
      document.getElementById('m-validity').textContent = btn.dataset.validity || '-';
      document.getElementById('m-ussd').textContent = btn.dataset.ussd ? ('*' + btn.dataset.ussd + '#') : '-';
      document.getElementById('m-amount').value = btn.dataset.amount;
      if (modal) modal.show();
    });
  });
}());
</script>
@endpush
```

> The Blade uses Bootstrap 5 classes and a `@push('scripts')` stack. If your panel
> uses a different CSS framework or layout, keep the field names
> (`operator`, `number`, `amount`, `product`) and the `data-*` attributes —
> those are what the controller and the filter JS depend on.

---

## 6. Quick checklist

1. Add the 5 `.env` keys and the `config/services.php` block.
2. Add the 3 routes.
3. Create `OfferController.php`.
4. Create `resources/views/offers/check.blade.php` (point `@extends` at your layout).
5. In `buyOfferProcess`, replace the commented block with your panel's existing
   recharge call.
6. Visit `/offers`, pick an operator, enter a number + amount, press **Check offers**.

Teletalk is not supported by the aggregator; the operator dropdown includes it but
it returns a "not supported" error — remove it from `$operatorChoices` if you prefer
not to show it.
