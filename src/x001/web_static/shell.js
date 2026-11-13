(() => {
  'use strict';
  const $ = id => document.getElementById(id);

  function updateClock() {
    $('clock').textContent = new Date().toLocaleString('zh-CN', {hour12: false});
  }

  async function checkDevice() {
    const label = $('side-device');
    const dot = $('side-dot');
    try {
      const response = await fetch('/api/device');
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '读取设备状态失败');
      label.textContent = data.connected ? `已连接 ${data.devices[0]}` : '手机未连接';
      dot.classList.toggle('on', data.connected);
    } catch (error) {
      label.textContent = '设备状态不可用';
      dot.classList.remove('on');
      console.error('读取设备状态失败：', error);
    }
  }

  updateClock();
  setInterval(updateClock, 1000);
  checkDevice();
  setInterval(checkDevice, 15000);
})();
