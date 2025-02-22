window.onload = function() {
    const toolContent = document.getElementById('json-formatter-content');
    
    // 创建工具界面
    toolContent.innerHTML = `
        <div class="row">
            <div class="col-md-6">
                <div class="form-group">
                    <label for="inputJson">输入JSON</label>
                    <textarea class="form-control" id="inputJson" rows="10"></textarea>
                </div>
                <button class="btn btn-primary mt-2" id="formatBtn">格式化</button>
            </div>
            <div class="col-md-6">
                <div class="form-group">
                    <label for="outputJson">格式化结果</label>
                    <textarea class="form-control" id="outputJson" rows="10" readonly></textarea>
                </div>
            </div>
        </div>
    `;

    // 添加功能
    const formatBtn = document.getElementById('formatBtn');
    const inputJson = document.getElementById('inputJson');
    const outputJson = document.getElementById('outputJson');

    formatBtn.addEventListener('click', function() {
        try {
            const parsed = JSON.parse(inputJson.value);
            outputJson.value = JSON.stringify(parsed, null, 2);
        } catch (e) {
            outputJson.value = '错误：无效的 JSON 格式';
        }
    });
}; 