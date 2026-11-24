$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'Video Localizer · 视觉模型配置'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size(530,270)
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
$ok = New-Object Windows.Forms.Button
$ok.Text = '保存'
$ok.SetBounds(415,220,96,28)
$ok.DialogResult = 'OK'
$form.AcceptButton = $ok
$form.Controls.Add($ok)
if ($form.ShowDialog() -ne 'OK') { exit 2 }
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::Write((@{base_url=$endpoint.Text;model=$model.Text;key=$key.Text} | ConvertTo-Json -Compress))
$form.Dispose()
