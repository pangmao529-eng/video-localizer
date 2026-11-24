$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$form = New-Object Windows.Forms.Form
$form.Text = 'Video Localizer · 视觉模型配置'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size(530,310)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
function Add-Field($title, $value, $top, $secret) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $title
    $label.SetBounds(16,$top,495,20)
    $form.Controls.Add($label)
    $box = New-Object Windows.Forms.TextBox
    $box.Text = $value
    $box.UseSystemPasswordChar = $secret
    $box.SetBounds(16,($top+22),495,26)
    $form.Controls.Add($box)
    return $box
}
$endpoint = Add-Field 'API 地址（例如 https://服务商域名/v1）' $env:VL_DIALOG_API 14 $false
$model = Add-Field '视觉模型 ID' $env:VL_DIALOG_MODEL 77 $false
$key = Add-Field 'API Key（只保存在此电脑，留空沿用该地址的凭据）' '' 140 $true
$message = New-Object Windows.Forms.Label
$message.SetBounds(16,205,495,46)
$message.Text = '请填写你选用的视觉模型服务商提供的地址、模型 ID 和密钥。'
$form.Controls.Add($message)
$ok = New-Object Windows.Forms.Button
$ok.Text = '保存'
$ok.SetBounds(415,260,96,32)
$ok.Add_Click({
    if ($form.Tag -eq 'saved') { $form.Close(); return }
    $ok.Enabled = $false
    try {
        $request = @{base_url=$endpoint.Text; model=$model.Text; key=$key.Text} | ConvertTo-Json -Compress
        [Console]::WriteLine($request)
        [Console]::Out.Flush()
        $response = [Console]::ReadLine()
        if ($null -eq $response) { throw 'Connection closed' }
        $result = ConvertFrom-Json -InputObject $response
        $message.Text = $result.message
        if ($result.ok) {
            $form.Tag = 'saved'
            $key.Clear()
            $endpoint.ReadOnly = $model.ReadOnly = $key.ReadOnly = $true
            $message.ForeColor = [Drawing.Color]::DarkGreen
            $ok.Text = '完成'
        } else {
            $message.ForeColor = [Drawing.Color]::Firebrick
        }
    } catch {
        $message.ForeColor = [Drawing.Color]::Firebrick
        $message.Text = '配置程序连接已断开。请重新打开窗口后再保存。'
    } finally {
        $ok.Enabled = $true
    }
})
$form.AcceptButton = $ok
$form.Controls.Add($ok)
[void]$form.ShowDialog()
$saved = $form.Tag -eq 'saved'
$form.Dispose()
if (-not $saved) { exit 2 }
