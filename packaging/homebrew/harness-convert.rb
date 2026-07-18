# frozen_string_literal: true

# Formula for harness-convert.
class HarnessConvert < Formula
  include Language::Python::Virtualenv

  desc "Relocate a coding-agent session across harnesses and resume it natively"
  homepage "https://hc.agentlab.in"
  url "https://github.com/harshitsinghbhandari/harness-convert/releases/download/v0.2.1/harness_convert-0.2.1.tar.gz"
  sha256 "40b73104a134dbd1380f0d2544f1fa626ffa0b2db3af94960640e9b2362ad67e"
  license "MIT"

  depends_on "python@3.14"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "relocate a coding-agent session", shell_output("#{bin}/hc --help")
  end
end
