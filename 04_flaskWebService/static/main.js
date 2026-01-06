// main.js - 구 선택 시 동 목록 업데이트

document.addEventListener('DOMContentLoaded', function() {
  const guSelect = document.getElementById('gu');
  const dongSelect = document.getElementById('dong');

  if (!guSelect || !dongSelect) {
    console.error('구 또는 동 select 요소를 찾을 수 없습니다.');
    return;
  }

  // 구 선택 시 동 목록 업데이트
  guSelect.addEventListener('change', function() {
    const selectedGu = this.value;
    
    // 동 선택 초기화
    dongSelect.innerHTML = '<option value="">선택해주세요</option>';
    
    if (!selectedGu) {
      dongSelect.disabled = true;
      return;
    }

    // 선택한 구의 동 목록 가져오기
    if (window.LOC && window.LOC[selectedGu]) {
      const dongs = Object.keys(window.LOC[selectedGu]).sort();
      
      dongs.forEach(function(dong) {
        const option = document.createElement('option');
        option.value = dong;
        option.textContent = dong;
        dongSelect.appendChild(option);
      });
      
      dongSelect.disabled = false;
    } else {
      dongSelect.disabled = true;
      console.error('선택한 구의 데이터를 찾을 수 없습니다:', selectedGu);
    }
  });
});
