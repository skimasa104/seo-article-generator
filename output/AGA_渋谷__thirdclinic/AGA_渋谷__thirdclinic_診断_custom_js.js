(function(){
  const answers = { q1: null, q2: null, q3: null };
  let currentStep = 1;

  const clinics = {
    'clinicfor': {
      name: 'クリニックフォア(オンライン診療)',
      reason: 'オンライン診療実績600万件以上を持つ大手クリニックグループのオンライン診療サービスで、渋谷エリアからも自宅・職場で診療と処方が完結します。診察料無料、定期配送のクーポン適用で予防プラン月1,049円〜とコスパが高く、副作用時の全額返金制度もあるため初めてのAGA治療でも始めやすいのが特徴です。',
      features: [
        '予防プラン月1,049円〜・発毛ライト月1,851円〜とコスパ重視の料金',
        '診察料無料、土日祝も含め毎日7:00〜24:00対応',
        '初めての治療で薬が体に合わなかった場合の全額返金制度あり',
        '取扱いAGA治療薬は国内最多水準で症状に合わせた処方が可能'
      ],
      aff: 'clinicfor'
    },
    'dmm': {
      name: 'DMMオンラインクリニック',
      reason: 'スマホ完結のオンライン専門クリニックで、渋谷エリアからも全国どこからでも自宅で診療と処方が完結します。診察料が無料、月額もリーズナブルな設定で、コスパと利便性を最大限重視する方に最適です。',
      features: [
        'オンライン完結で通院時間ゼロ、お薬は最短当日発送',
        '診察料無料、12ヶ月プランで月額単価を大きく下げられる',
        '渋谷エリア外への引越し・転勤があっても継続OK',
        '24時間いつでも予約・受診できるカジュアルな運用'
      ],
      aff: 'dmm'
    },
    'agaskin': {
      name: 'AGAスキンクリニック渋谷駅前院',
      reason: '各線「渋谷駅」から徒歩1分・道玄坂2-3-1のAGA・FAGA専門クリニックで、人通りの多い渋谷でも通いやすいアクセスです。カウンセリングから会計まで完全個室・完全予約制で周囲の目を気にせず受けられる環境で、内服薬から独自の発毛薬「Rebirth」まで幅広いオーダーメイド治療に対応しています。本気でAGA治療に取り組みたい方に向いています。',
      features: [
        '各線「渋谷駅」徒歩1分・道玄坂の好アクセス',
        '独自の発毛薬「Rebirth」など内服・外用・注入のオーダーメイド治療',
        '60分の無料カウンセリング、初診・再診料すべて無料',
        '土日祝も診療対応(年中無休)で仕事帰りの通院も可能'
      ],
      aff: 'agaskin'
    },
    'leva': {
      name: 'レバクリ',
      reason: 'オンライン完結で長期継続しやすいコスパ重視のAGAクリニックです。予防プランは月1,349円〜、発毛プランは月1,650円〜と費用を抑えられ、2年目以降も同価格で続けられる点が大きな特徴です。LINEで気軽に予約・相談でき、渋谷エリアからも自宅で診療と処方が完結します。',
      features: [
        '予防プラン月1,349円〜・発毛プラン月1,650円〜の業界最安水準',
        '2年目以降も同じ価格で継続可能、長期治療に最適',
        '初診料0円・全額返金保証ありで初めてのAGA治療も安心',
        '定期配送は送料無料、LINEで手軽に予約・相談できる'
      ],
      aff: 'leva'
    }
  };

  function decide(){
    const q1 = answers.q1, q2 = answers.q2, q3 = answers.q3;
    if(q1 === 'face') return 'agaskin';
    if(q1 === 'online'){
      if(q2 === 'low' || q3 === 'price') return 'leva';
      return 'dmm';
    }
    if(q1 === 'hybrid'){
      if(q3 === 'price' || q2 === 'low') return 'leva';
      if(q3 === 'treatment' || q2 === 'high') return 'agaskin';
      return 'clinicfor';
    }
    return 'clinicfor';
  }

  function showResult(){
    const data = clinics[decide()];
    document.getElementById('result-name').textContent = data.name;
    document.getElementById('result-reason').textContent = data.reason;
    const ul = document.getElementById('result-features');
    ul.innerHTML = '';
    data.features.forEach(f => {
      const li = document.createElement('li');
      li.textContent = f;
      ul.appendChild(li);
    });
    const cta = document.getElementById('result-cta');
    cta.setAttribute('data-aff', data.aff);
    // 既存のaff-btnハンドラを無効化するためクラスを一旦剥がす
    cta.classList.remove('aff-btn', 'aff-btn--block');
    // 既存ハンドラ削除のためにノードを複製で置換
    const newCta = cta.cloneNode(true);
    cta.parentNode.replaceChild(newCta, cta);
    newCta.classList.add('aga-result-cta'); // 念のため再付与
    newCta.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      const affKey = data.aff;
      if(window.AFF_LINKS && window.AFF_LINKS[affKey]){
        const w = window.open(window.AFF_LINKS[affKey], '_blank', 'noopener,noreferrer');
        if(w) w.opener = null;
      } else if(typeof window.openAff === 'function'){
        window.openAff(affKey);
      } else {
        console.warn('[aff] AFF_LINKSが未公開、またはキーが未登録:', affKey);
      }
    }, true); // capture phaseで優先実行
    document.querySelectorAll('.aga-step').forEach(s => s.classList.remove('active'));
    document.getElementById('result').classList.add('active');
    for(let i=1; i<=3; i++){
      const bar = document.getElementById('bar-'+i);
      bar.classList.remove('active');
      bar.classList.add('done');
    }
    document.getElementById('line-1').classList.add('done');
    document.getElementById('line-2').classList.add('done');
    document.getElementById('aga-diagnosis').scrollIntoView({behavior:'smooth', block:'start'});
  }

  function nextStep(stepNum, answer){
    answers['q'+stepNum] = answer;
    const currentBar = document.getElementById('bar-'+stepNum);
    currentBar.classList.remove('active');
    currentBar.classList.add('done');
    if(stepNum < 3){
      document.getElementById('line-'+stepNum).classList.add('done');
      document.getElementById('bar-'+(stepNum+1)).classList.add('active');
      document.getElementById('step-'+stepNum).classList.remove('active');
      document.getElementById('step-'+(stepNum+1)).classList.add('active');
      currentStep = stepNum + 1;
    } else {
      showResult();
    }
  }

  // クリック+キーボード両対応
  function bindOption(el, stepNum){
    const handler = () => nextStep(stepNum, el.dataset.answer);
    el.addEventListener('click', handler);
    el.addEventListener('keydown', (e) => {
      if(e.key === 'Enter' || e.key === ' '){
        e.preventDefault();
        handler();
      }
    });
  }

  document.querySelectorAll('#step-1 .aga-option').forEach(el => bindOption(el, 1));
  document.querySelectorAll('#step-2 .aga-option').forEach(el => bindOption(el, 2));
  document.querySelectorAll('#step-3 .aga-option').forEach(el => bindOption(el, 3));

  const restart = document.getElementById('restart');
  const restartHandler = () => {
    answers.q1 = answers.q2 = answers.q3 = null;
    document.getElementById('result').classList.remove('active');
    document.querySelectorAll('.aga-progress-bar').forEach(b => {
      b.classList.remove('done', 'active');
    });
    document.querySelectorAll('.aga-progress-line').forEach(l => l.classList.remove('done'));
    document.getElementById('bar-1').classList.add('active');
    document.querySelectorAll('.aga-step').forEach(s => s.classList.remove('active'));
    document.getElementById('step-1').classList.add('active');
    currentStep = 1;
  };
  restart.addEventListener('click', restartHandler);
  restart.addEventListener('keydown', (e) => {
    if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); restartHandler(); }
  });
})();
