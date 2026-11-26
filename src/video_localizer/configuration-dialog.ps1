$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$form = New-Object Windows.Forms.Form
$form.Text = 'Video Localizer · 模型配置'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size(530,438)
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
$providerLabel = New-Object Windows.Forms.Label
$providerLabel.Text = '模型提供方'
$providerLabel.SetBounds(16,14,495,20)
$form.Controls.Add($providerLabel)
$provider = New-Object Windows.Forms.ComboBox
$provider.DropDownStyle = 'DropDownList'
$provider.SetBounds(16,36,495,26)
[void]$provider.Items.Add('hunyuan')
[void]$provider.Items.Add('seedance')
$provider.SelectedItem = if ($env:VL_DIALOG_PROVIDER -eq 'seedance') { 'seedance' } else { 'hunyuan' }
$form.Controls.Add($provider)
$endpoint = Add-Field 'API 地址' $env:VL_DIALOG_API 77 $false
$model = Add-Field '模型 ID / 推理接入点 ID（ep-...）' $env:VL_DIALOG_MODEL 140 $false
$familyLabel = New-Object Windows.Forms.Label
$familyLabel.Text = 'Seedance 系列（接入点 ID 必须选择）'
$familyLabel.SetBounds(16,203,495,20)
$form.Controls.Add($familyLabel)
$family = New-Object Windows.Forms.ComboBox
$family.DropDownStyle = 'DropDownList'
$family.SetBounds(16,225,495,26)
[void]$family.Items.Add('seedance-2.0')
[void]$family.Items.Add('seedance-2.5')
$form.Controls.Add($family)
$key = Add-Field 'API Key（只保存在此电脑，留空沿用该地址的凭据）' '' 266 $true
$message = New-Object Windows.Forms.Label
$message.SetBounds(16,331,495,46)
$form.Controls.Add($message)
$drafts = @{
    hunyuan = @{base_url='https://tokenhub.tencentmaas.com/v1'; model='hy4'; model_family=''}
    seedance = @{base_url='https://ark.cn-beijing.volces.com/api/v3'; model='doubao-seedance-2-0-260128'; model_family='seedance-2.0'}
}
$script:currentProvider = [string]$provider.SelectedItem
$drafts[$script:currentProvider] = @{base_url=$endpoint.Text; model=$model.Text; model_family=$env:VL_DIALOG_FAMILY}
$script:updatingFamily = $false
$script:restoringDraft = $false
$script:currentModelId = $model.Text.Trim()
function Set-FamilyForModel($fallback) {
    $script:updatingFamily = $true
    try {
        $family.SelectedIndex = -1
        $family.SelectedItem = $null
        if ($provider.SelectedItem -eq 'seedance') {
            if ($model.Text.Trim() -eq 'doubao-seedance-2-0-260128') {
                $family.SelectedItem = 'seedance-2.0'
            } elseif ($model.Text.Trim() -eq 'doubao-seedance-2-5-260628') {
                $family.SelectedItem = 'seedance-2.5'
            } elseif ($fallback -in @('seedance-2.0','seedance-2.5')) {
                $family.SelectedItem = $fallback
            }
        }
    } finally {
        $script:updatingFamily = $false
    }
}
function Update-ProviderText {
    $displayName = if ($provider.SelectedItem -eq 'seedance') { 'Seedance' } else { '混元' }
    $form.Text = "Video Localizer · $displayName 模型配置"
    $message.ForeColor = [Drawing.SystemColors]::ControlText
    $message.Text = "$displayName 配置只保存在本机。保存不代表连接已通过，也不会自动生成视频。"
    $family.Enabled = $familyLabel.Enabled = $provider.SelectedItem -eq 'seedance'
}
function Update-FamilyForModelChange {
    if ($script:restoringDraft) { return }
    $nextModelId = $model.Text.Trim()
    if ($nextModelId -ceq $script:currentModelId) { return }
    $script:currentModelId = $nextModelId
    # A new deployment must not inherit the capabilities of the previous ID.
    Set-FamilyForModel ''
    if ($provider.SelectedItem -eq 'seedance') {
        if ($null -eq $family.SelectedItem) {
            $message.Text = '模型或接入点 ID 已变更，请重新选择 Seedance 系列后保存。'
        } else {
            Update-ProviderText
        }
    }
}
function Restore-ProviderDraft {
    $drafts[$script:currentProvider] = @{base_url=$endpoint.Text; model=$model.Text; model_family=[string]$family.SelectedItem}
    $script:currentProvider = [string]$provider.SelectedItem
    $script:restoringDraft = $true
    try {
        $endpoint.Text = $drafts[$script:currentProvider].base_url
        $model.Text = $drafts[$script:currentProvider].model
        Set-FamilyForModel $drafts[$script:currentProvider].model_family
        $script:currentModelId = $model.Text.Trim()
    } finally {
        $script:restoringDraft = $false
    }
    $key.Clear()
    Update-ProviderText
}
Set-FamilyForModel $env:VL_DIALOG_FAMILY
Update-ProviderText
$model.Add_TextChanged({ Update-FamilyForModelChange })
$family.Add_SelectedIndexChanged({
    if (-not $script:updatingFamily -and $provider.SelectedItem -eq 'seedance' -and $model.Text.Trim() -in @('doubao-seedance-2-0-260128','doubao-seedance-2-5-260628')) {
        if ($family.SelectedItem -eq 'seedance-2.0') { $model.Text = 'doubao-seedance-2-0-260128' }
        elseif ($family.SelectedItem -eq 'seedance-2.5') { $model.Text = 'doubao-seedance-2-5-260628' }
    }
})
$provider.Add_SelectedIndexChanged({ Restore-ProviderDraft })
$ok = New-Object Windows.Forms.Button
$ok.Text = '保存'
$ok.SetBounds(415,388,96,32)
$ok.Add_Click({
    if ($form.Tag -eq 'saved') { $form.Close(); return }
    $ok.Enabled = $false
    try {
        $fields = @{provider=[string]$provider.SelectedItem; base_url=$endpoint.Text; model=$model.Text; key=$key.Text}
        if ($provider.SelectedItem -eq 'seedance') { $fields.model_family = [string]$family.SelectedItem }
        $request = $fields | ConvertTo-Json -Compress
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
            $provider.Enabled = $false
            $family.Enabled = $false
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
